"""OEE Command Center console: FastAPI backend over the Exasol OEE schema.

Every endpoint runs SQL against Exasol and returns the rows together with the
exact SQL text and the measured query time, so the console can show what the
database is doing.

Run:  python -m oee.cli serve      (then open http://127.0.0.1:8000)
"""
from __future__ import annotations

import datetime as _dt
import decimal
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from oee import assess as _assess
from oee.assess import MODES
from oee.features import FEATURES

STATIC = Path(__file__).resolve().parent / "static"

_PLANT_RE = re.compile(r"^[A-Z]{3}$")
_LINE_RE = re.compile(r"^[A-Z]{3}-L\d{1,2}$")
_MACHINE_RE = re.compile(r"^[A-Z]{3}-[A-Z]{3,6}-\d{2}$")

# Features a user may change in the what-if panel, with sane bounds
WHATIF_BOUNDS = {
    "TOOL_WEAR_MIN": (0.0, 400.0),
    "VIBRATION_MM_S": (0.5, 12.0),
    "TEMP_DIFF_K": (2.0, 16.0),
    "TORQUE_RATIO": (0.4, 2.5),
}


class WhatIf(BaseModel):
    machine_id: str
    overrides: dict[str, float] = {}


class NewWorkOrder(BaseModel):
    machine_id: str


_WO_SQL = """
SELECT WO_ID, MACHINE_ID, PLANT_ID, FAILURE_MODE, PART_NO, RISK_AT_OPEN, OPENED_AT, STATUS
FROM OEE.WORK_ORDERS
WHERE STATUS = 'OPEN'
ORDER BY OPENED_AT DESC"""


def _clean(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat(sep=" ") if isinstance(v, _dt.datetime) else v.isoformat()
    return v


class ExasolBackend:
    """One shared pyexasol connection, serialised with a lock (pyexasol is not thread-safe)."""

    name = "Exasol"

    def __init__(self):
        self._conn = None
        self._lock = threading.Lock()

    def _connection(self):
        if self._conn is None or self._conn.is_closed:
            import pyexasol

            from oee.db import connect

            # Map DECIMAL/DATE/TIMESTAMP to Python types (raw values can arrive as strings)
            self._conn = connect(fetch_mapper=pyexasol.exasol_mapper)
        return self._conn

    def query(self, sql: str) -> list[dict]:
        import pyexasol

        with self._lock:
            try:
                stmt = self._connection().execute(sql)
            except pyexasol.ExaQueryError as exc:
                if "not found" in str(exc.message):
                    raise HTTPException(503, f"{exc.message}. Run `python -m oee.cli deploy` to create the views.")
                raise HTTPException(500, str(exc.message))
            cols = [c.upper() for c in stmt.column_names()]
            return [dict(zip(cols, map(_clean, row))) for row in stmt.fetchall()]

    def execute(self, sql: str) -> int:
        """Run a statement that returns no rows (INSERT/UPDATE); returns affected row count."""
        with self._lock:
            stmt = self._connection().execute(sql)
            return stmt.rowcount()


def _run(backend, sql: str) -> dict:
    sql = sql.strip()
    t0 = time.perf_counter()
    rows = backend.query(sql)
    ms = (time.perf_counter() - t0) * 1000.0
    return {"rows": rows, "sql": sql, "ms": round(ms, 1)}


def _check(value: str | None, pattern: re.Pattern, what: str) -> str | None:
    if value is None or value == "":
        return None
    v = value.upper()
    if not pattern.match(v):
        raise HTTPException(status_code=400, detail=f"Invalid {what}: {value!r}")
    return v


# Time lost in each OEE category, in minutes. Planned time minus the three
# losses equals fully productive time (ideal cycle x good parts).
_LOSS_COLUMNS = """
    SUM(PLANNED_MINUTES)                                         AS PLANNED_MIN,
    SUM(DOWNTIME_MINUTES)                                        AS AVAILABILITY_LOSS_MIN,
    SUM(RUN_MINUTES) - SUM(IDEAL_CYCLE_S * TOTAL_COUNT) / 60.0   AS PERFORMANCE_LOSS_MIN,
    SUM(IDEAL_CYCLE_S * (TOTAL_COUNT - GOOD_COUNT)) / 60.0       AS QUALITY_LOSS_MIN,
    SUM(IDEAL_CYCLE_S * GOOD_COUNT) / 60.0                       AS PRODUCTIVE_MIN,
    SUM(RUN_MINUTES) / SUM(PLANNED_MINUTES)                      AS AVAILABILITY,
    SUM(IDEAL_CYCLE_S * TOTAL_COUNT) / (SUM(RUN_MINUTES) * 60)   AS PERFORMANCE,
    SUM(IDEAL_CYCLE_S * GOOD_COUNT) / SUM(IDEAL_CYCLE_S * TOTAL_COUNT)                   AS QUALITY,
    SUM(IDEAL_CYCLE_S * GOOD_COUNT) / 60.0 / SUM(PLANNED_MINUTES) AS OEE"""


def create_app(backend_factory: Callable[[], Any] = ExasolBackend) -> FastAPI:
    app = FastAPI(title="OEE Command Center", docs_url="/api/docs", redoc_url=None)
    backend = backend_factory()

    @app.get("/api/health")
    def health():
        out = _run(backend, """
SELECT
    (SELECT COUNT(*) FROM OEE.TELEMETRY)  AS TELEMETRY_ROWS,
    (SELECT COUNT(*) FROM OEE.RISK_SCORES) AS SCORED_ROWS,
    (SELECT COUNT(*) FROM OEE.MACHINES)   AS MACHINES,
    (SELECT MIN(READING_TS) FROM OEE.TELEMETRY) AS FIRST_TS,
    (SELECT MAX(READING_TS) FROM OEE.TELEMETRY) AS LAST_TS""")
        out["backend"] = getattr(backend, "name", "Exasol")
        return out

    @app.get("/api/model")
    def model():
        return _run(backend, """
SELECT MODEL_NAME, DEPLOYED_AT, N_TREES, TEST_AUC, TEST_PRECISION_AT_K
FROM OEE.MODEL_REGISTRY
ORDER BY CASE WHEN MODEL_NAME = 'FAILURE_RISK' THEN 0 ELSE 1 END, DEPLOYED_AT DESC""")

    @app.get("/api/oee/losses")
    def oee_losses(level: str = "plant", plant: str | None = None, line: str | None = None):
        """OEE and its loss breakdown, grouped by plant, line or machine."""
        group = {"plant": "PLANT_ID", "line": "LINE_ID", "machine": "MACHINE_ID"}.get(level)
        if group is None:
            raise HTTPException(400, "level must be plant, line or machine")
        p = _check(plant, _PLANT_RE, "plant")
        ln = _check(line, _LINE_RE, "line")
        where = []
        if p:
            where.append(f"PLANT_ID = '{p}'")
        if ln:
            where.append(f"LINE_ID = '{ln}'")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        extra = ", MAX(MACHINE_TYPE) AS MACHINE_TYPE, MAX(LINE_ID) AS LINE_ID" if group == "MACHINE_ID" else ""
        return _run(backend, f"""
SELECT {group} AS UNIT_ID{extra},{_LOSS_COLUMNS}
FROM OEE.V_OEE_SHIFT
{where_sql}
GROUP BY {group}
ORDER BY OEE""")

    @app.get("/api/oee/daily")
    def oee_daily(level: str = "plant", plant: str | None = None, line: str | None = None,
                  machine: str | None = None):
        """Daily OEE grouped by plant, line or machine, with the same filters as /api/oee/losses."""
        key = {"plant": "PLANT_ID", "line": "LINE_ID", "machine": "MACHINE_ID"}.get(level)
        if key is None:
            raise HTTPException(400, "level must be plant, line or machine")
        p = _check(plant, _PLANT_RE, "plant")
        ln = _check(line, _LINE_RE, "line")
        m = _check(machine, _MACHINE_RE, "machine")
        conds = []
        if p:
            conds.append(f"PLANT_ID = '{p}'")
        if ln:
            conds.append(f"LINE_ID = '{ln}'")
        if m:
            conds.append(f"MACHINE_ID = '{m}'")
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        return _run(backend, f"""
SELECT {key} AS UNIT_ID, SHIFT_DATE,
    SUM(RUN_MINUTES) / SUM(PLANNED_MINUTES)                       AS AVAILABILITY,
    SUM(IDEAL_CYCLE_S * TOTAL_COUNT) / (SUM(RUN_MINUTES) * 60)    AS PERFORMANCE,
    SUM(IDEAL_CYCLE_S * GOOD_COUNT) / SUM(IDEAL_CYCLE_S * TOTAL_COUNT)                    AS QUALITY,
    SUM(IDEAL_CYCLE_S * GOOD_COUNT) / 60.0 / SUM(PLANNED_MINUTES) AS OEE
FROM OEE.V_OEE_SHIFT
{where}
GROUP BY {key}, SHIFT_DATE
ORDER BY {key}, SHIFT_DATE""")

    @app.get("/api/risk")
    def risk(limit: int = 24):
        limit = max(1, min(int(limit), 200))
        return _run(backend, f"""
SELECT MACHINE_ID, PLANT_ID, MACHINE_TYPE, READING_TS, RISK_24H,
       TOOL_WEAR_MIN, VIBRATION_MM_S, TEMP_DIFF_K, MTBF_HOURS, MTTR_HOURS, FAILURES
FROM OEE.V_RISK_LATEST
ORDER BY RISK_24H DESC
LIMIT {limit}""")

    @app.get("/api/machine/{machine_id}")
    def machine(machine_id: str):
        m = _check(machine_id, _MACHINE_RE, "machine")
        series = _run(backend, f"""
SELECT READING_TS, RISK_24H, TOOL_WEAR_MIN, VIBRATION_MM_S, TEMP_DIFF_K, TORQUE_RATIO
FROM OEE.RISK_SCORES
WHERE MACHINE_ID = '{m}'
ORDER BY READING_TS""")
        stops = _run(backend, f"""
SELECT d.STOP_ID, d.START_TS, d.END_TS, d.STOP_TYPE, d.FAILURE_MODE, d.PART_REPLACED,
       SECONDS_BETWEEN(d.END_TS, d.START_TS) / 3600.0 AS STOP_HOURS,
       n.NOTE_TEXT
FROM OEE.DOWNTIME d
LEFT JOIN OEE.MAINT_NOTES n ON n.STOP_ID = d.STOP_ID
WHERE d.MACHINE_ID = '{m}'
ORDER BY d.START_TS DESC""")
        info = _run(backend, f"""
SELECT m.MACHINE_ID, m.PLANT_ID, m.LINE_ID, m.MACHINE_TYPE, m.INSTALL_YEAR,
       r.OPERATING_HOURS, r.FAILURES, r.MTBF_HOURS, r.MTTR_HOURS
FROM OEE.MACHINES m
JOIN OEE.V_RELIABILITY r ON r.MACHINE_ID = m.MACHINE_ID
WHERE m.MACHINE_ID = '{m}'""")
        if not info["rows"]:
            raise HTTPException(404, f"Unknown machine {m}")
        return {"info": info, "series": series, "stops": stops}

    @app.get("/api/parts")
    def parts():
        return _run(backend, """
SELECT PLANT_ID, PART_NO, ON_HAND, REORDER_POINT, LEAD_TIME_DAYS, UNIT_COST_INR,
       EXPECTED_DEMAND_24H, STATUS
FROM OEE.V_PARTS_EXPOSURE
ORDER BY CASE STATUS WHEN 'STOCKOUT_RISK' THEN 0 WHEN 'LOW' THEN 1 ELSE 2 END,
         EXPECTED_DEMAND_24H DESC""")

    @app.get("/api/pareto")
    def pareto(plant: str | None = None):
        p = _check(plant, _PLANT_RE, "plant")
        where = f"WHERE PLANT_ID = '{p}'" if p else ""
        return _run(backend, f"""
SELECT FAILURE_MODE, SUM(STOPS) AS STOPS, SUM(DOWN_HOURS) AS DOWN_HOURS
FROM OEE.V_DOWNTIME_PARETO
{where}
GROUP BY FAILURE_MODE
ORDER BY DOWN_HOURS DESC""")

    @app.post("/api/whatif")
    def whatif(req: WhatIf):
        """Score the machine's latest reading, with some inputs changed, through the live UDF."""
        m = _check(req.machine_id, _MACHINE_RE, "machine")
        exprs = []
        for f in FEATURES:
            if f in req.overrides:
                if f not in WHATIF_BOUNDS:
                    raise HTTPException(400, f"{f} cannot be changed")
                lo, hi = WHATIF_BOUNDS[f]
                v = min(max(float(req.overrides[f]), lo), hi)
                exprs.append(f"CAST({v:.6f} AS DOUBLE)")
            elif f == "WEAR_X_LOAD" and ("TOOL_WEAR_MIN" in req.overrides or "TORQUE_RATIO" in req.overrides):
                # keep the derived interaction feature consistent with edited inputs
                w = req.overrides.get("TOOL_WEAR_MIN")
                r = req.overrides.get("TORQUE_RATIO")
                w_sql = f"CAST({float(w):.6f} AS DOUBLE)" if w is not None else "TOOL_WEAR_MIN"
                r_sql = f"CAST({float(r):.6f} AS DOUBLE)" if r is not None else "TORQUE_RATIO"
                exprs.append(f"{w_sql} * {r_sql}")
            else:
                exprs.append(f)
        args = ",\n    ".join(exprs)
        return _run(backend, f"""
SELECT MACHINE_ID, READING_TS,
  OEE.FAILURE_RISK(
    {args}
  ) AS RISK_24H
FROM OEE.V_FEATURES
WHERE MACHINE_ID = '{m}'
  AND READING_TS = (SELECT MAX(READING_TS) FROM OEE.TELEMETRY WHERE MACHINE_ID = '{m}')""")

    def _assessment(machine: str | None = None):
        """Per-machine verdicts, computed by the diagnosis views in Exasol."""
        status, modes, signals = (_run(backend, q) for q in _assess.queries(machine))
        machines = _assess.shape(status["rows"], modes["rows"], signals["rows"])
        meta = [{"sql": q["sql"], "ms": q["ms"]} for q in (status, modes, signals)]
        return machines, meta

    @app.get("/api/fleet")
    def fleet():
        """Every machine's latest verdict: risk, status, likely mode, parts, pipeline stage."""
        machines, meta = _assessment()
        return {"machines": machines, "queries": meta}

    @app.get("/api/diagnosis/{machine_id}")
    def diagnosis(machine_id: str):
        m = _check(machine_id, _MACHINE_RE, "machine")
        machines, meta = _assessment(m)
        if not machines:
            raise HTTPException(404, f"Unknown machine {m}")
        a = machines[0]
        top = a["likely_modes"][0]["mode"]
        similar = _run(backend, f"""
SELECT d.STOP_ID, d.MACHINE_ID, m.MACHINE_TYPE, d.START_TS, d.FAILURE_MODE, d.PART_REPLACED,
       SECONDS_BETWEEN(d.END_TS, d.START_TS) / 3600.0 AS STOP_HOURS, n.NOTE_TEXT,
       CASE WHEN m.MACHINE_TYPE = '{a["machine_type"]}' THEN 0 ELSE 1 END AS SAME_TYPE_RANK
FROM OEE.DOWNTIME d
JOIN OEE.MACHINES m ON m.MACHINE_ID = d.MACHINE_ID
LEFT JOIN OEE.MAINT_NOTES n ON n.STOP_ID = d.STOP_ID
WHERE d.STOP_TYPE = 'UNPLANNED' AND d.FAILURE_MODE = '{top}'
ORDER BY SAME_TYPE_RANK, d.START_TS DESC
LIMIT 3""")
        part_list = ", ".join(f"'{x['part_no']}'" for x in a["likely_modes"][:3])
        parts = _run(backend, f"""
SELECT PART_NO, ON_HAND, REORDER_POINT, LEAD_TIME_DAYS, UNIT_COST_INR
FROM OEE.SPARE_PARTS
WHERE PLANT_ID = '{a["plant_id"]}' AND PART_NO IN ({part_list})""")
        order = [x["part_no"] for x in a["likely_modes"][:3]]
        parts["rows"].sort(key=lambda r: order.index(r["PART_NO"]) if r["PART_NO"] in order else 99)
        return {"machine": a, "similar": similar, "parts": parts, "queries": meta}

    @app.get("/api/history/{mode}")
    def history(mode: str):
        md = mode.upper()
        if md not in MODES:
            raise HTTPException(400, f"Unknown failure mode {mode!r}")
        return _run(backend, f"""
SELECT d.STOP_ID, d.MACHINE_ID, m.MACHINE_TYPE, m.PLANT_ID, d.START_TS, d.PART_REPLACED,
       SECONDS_BETWEEN(d.END_TS, d.START_TS) / 3600.0 AS STOP_HOURS, n.NOTE_TEXT
FROM OEE.DOWNTIME d
JOIN OEE.MACHINES m ON m.MACHINE_ID = d.MACHINE_ID
LEFT JOIN OEE.MAINT_NOTES n ON n.STOP_ID = d.STOP_ID
WHERE d.STOP_TYPE = 'UNPLANNED' AND d.FAILURE_MODE = '{md}'
ORDER BY d.START_TS DESC
LIMIT 12""")

    @app.get("/api/workorders")
    def workorders():
        return _run(backend, _WO_SQL)

    @app.post("/api/workorders")
    def raise_workorder(req: NewWorkOrder):
        """Raise a work order for the machine's most likely failure mode (written to Exasol)."""
        m = _check(req.machine_id, _MACHINE_RE, "machine")
        machines, _ = _assessment(m)
        if not machines:
            raise HTTPException(404, f"Unknown machine {m}")
        a = machines[0]
        if a["work_order"]:
            raise HTTPException(409, f"{m} already has open work order {int(a['work_order']['WO_ID'])}")
        next_id = _run(backend, "SELECT COALESCE(MAX(WO_ID), 0) + 1 AS NEXT_ID FROM OEE.WORK_ORDERS")["rows"][0]["NEXT_ID"]
        # The work order is filled straight from the diagnosis view: likely mode, part and risk
        sql = f"""
INSERT INTO OEE.WORK_ORDERS (WO_ID, MACHINE_ID, PLANT_ID, FAILURE_MODE, PART_NO, RISK_AT_OPEN, OPENED_AT, STATUS)
SELECT {int(next_id)}, MACHINE_ID, PLANT_ID, TOP_MODE, PART_NO, RISK_24H, CURRENT_TIMESTAMP, 'OPEN'
FROM OEE.V_MACHINE_STATUS
WHERE MACHINE_ID = '{m}'""".strip()
        # Reserve every part of the repair kit at its fastest source (09_sourcing.sql)
        reserve_sql = f"""
INSERT INTO OEE.PART_RESERVATIONS (WO_ID, PART_NO, QTY, FROM_LOCATION, TO_PLANT, SOURCE_KIND, ETA_HOURS, RESERVED_AT, STATUS)
SELECT {int(next_id)}, PART_NO, QTY, FROM_LOCATION, PLANT_ID, SOURCE_KIND, ETA_HOURS, CURRENT_TIMESTAMP, 'RESERVED'
FROM OEE.V_MACHINE_SOURCING
WHERE MACHINE_ID = '{m}'""".strip()
        t0 = time.perf_counter()
        backend.execute(sql)
        backend.execute(reserve_sql)
        ms = (time.perf_counter() - t0) * 1000.0
        return {"wo_id": int(next_id), "sql": sql + ";\n\n" + reserve_sql, "ms": round(ms, 1)}

    @app.post("/api/workorders/{wo_id}/close")
    def close_workorder(wo_id: int):
        sql = f"UPDATE OEE.WORK_ORDERS SET STATUS = 'CLOSED' WHERE WO_ID = {int(wo_id)}"
        release = f"UPDATE OEE.PART_RESERVATIONS SET STATUS = 'RELEASED' WHERE WO_ID = {int(wo_id)}"
        t0 = time.perf_counter()
        backend.execute(sql)
        backend.execute(release)
        sql = sql + ";\n" + release
        return {"sql": sql, "ms": round((time.perf_counter() - t0) * 1000.0, 1)}

    @app.get("/api/fusion")
    def fusion():
        """Step 1: the five scattered sensor sources, how complete the fused data is, and what it is worth."""
        health = _run(backend, """
SELECT ORD, SOURCE_NAME, STREAM, SOURCE_FORMAT, RAW_ROWS, AVG_DELAY_MIN, MEASURED_SHARE, COMPLETE_SHARE
FROM OEE.V_SOURCE_HEALTH
ORDER BY ORD""")
        impact = _run(backend, """
SELECT ORD, PIPELINE, HOURS_SCORED, STOPS, STOPS_WARNED, CATCH_RATE, AVG_WARNING_HOURS, ALERT_HOURS, ALERT_PRECISION
FROM OEE.FUSION_IMPACT
ORDER BY ORD""")
        return {"health": health, "impact": impact}

    @app.get("/api/rootcause/{machine_id}")
    def rootcause(machine_id: str):
        """Step 2: per-stream patterns for the machine's latest hour, the root cause, and held-out accuracy."""
        m = _check(machine_id, _MACHINE_RE, "machine")
        patterns = _run(backend, f"""
SELECT STREAM, STREAM_ORDER, PATTERN, BAD_Z, ACTIVE
FROM OEE.V_STREAM_PATTERNS_LATEST
WHERE MACHINE_ID = '{m}'
ORDER BY STREAM_ORDER""")
        causes = _run(backend, f"""
SELECT FAILURE_MODE, CONFIDENCE, CAUSE_RANK, D2, PRIOR_SHARE, READING_TS
FROM OEE.V_ROOT_CAUSE_LATEST
WHERE MACHINE_ID = '{m}'
ORDER BY CAUSE_RANK""")
        signatures = _run(backend, """
SELECT FAILURE_MODE, STREAM, STREAM_ORDER, SIGNATURE_Z, LEARNED_FROM_STOPS
FROM OEE.FAULT_SIGNATURES
ORDER BY FAILURE_MODE, STREAM_ORDER""")
        accuracy = _run(backend, "SELECT * FROM OEE.V_ROOT_CAUSE_ACCURACY")
        return {"patterns": patterns, "causes": causes, "signatures": signatures, "accuracy": accuracy}

    @app.get("/api/sourcing")
    def sourcing(machine: str | None = None):
        """Step 3: fastest source for every part of each machine's repair kit, plus the fleet impact."""
        where = ""
        if machine:
            where = f"WHERE MACHINE_ID = '{_check(machine, _MACHINE_RE, 'machine')}'"
        plan = _run(backend, f"""
SELECT MACHINE_ID, PLANT_ID, HEALTH, RISK_24H, TOP_MODE, OPEN_WO_ID, PART_NO, QTY, SOURCE_KIND, FROM_LOCATION,
       ETA_HOURS, COST_INR, SUPPLIER_HOURS, LOCAL_ONLY_HOURS
FROM OEE.V_MACHINE_SOURCING
{where}
ORDER BY RISK_24H DESC, MACHINE_ID, PART_NO""".replace("\n\n", "\n"))
        impact = _run(backend, "SELECT * FROM OEE.V_SOURCING_IMPACT")
        options = _run(backend, f"""
SELECT o.PLANT_ID, o.FAILURE_MODE, o.PART_NO, o.QTY, o.SOURCE_KIND, o.FROM_LOCATION, o.ETA_HOURS, o.COST_INR,
       o.AVAILABLE, o.OPTION_RANK
FROM OEE.V_SOURCING_OPTIONS o
JOIN OEE.V_MACHINE_STATUS s ON s.PLANT_ID = o.PLANT_ID AND s.TOP_MODE = o.FAILURE_MODE
{where.replace('MACHINE_ID', 's.MACHINE_ID')}
ORDER BY o.PART_NO, o.OPTION_RANK""") if machine else None
        reservations = _run(backend, """
SELECT r.WO_ID, w.MACHINE_ID, r.PART_NO, r.QTY, r.FROM_LOCATION, r.TO_PLANT, r.SOURCE_KIND, r.ETA_HOURS, r.STATUS
FROM OEE.PART_RESERVATIONS r JOIN OEE.WORK_ORDERS w ON w.WO_ID = r.WO_ID
WHERE r.STATUS = 'RESERVED'
ORDER BY r.WO_ID DESC""")
        return {"plan": plan, "impact": impact, "options": options, "reservations": reservations}

    @app.get("/api/quality")
    def quality():
        """Step 4: next-shift scrap risk per machine (OEE.QUALITY_RISK UDF) and held-out impact."""
        nxt = _run(backend, """
SELECT MACHINE_ID, PLANT_ID, MACHINE_TYPE, NEXT_SHIFT_START, TOOL_WEAR_MIN, VIB_AVG_6H, TEMP_DIFF_AVG_6H,
       PREV_SCRAP_RATE, QUALITY_RISK, RECOMMENDATION
FROM OEE.V_QUALITY_NEXT_SHIFT
ORDER BY QUALITY_RISK DESC""")
        impact = _run(backend, "SELECT * FROM OEE.QUALITY_IMPACT ORDER BY ORD")
        return {"next_shift": nxt, "impact": impact}

    @app.get("/api/suppliers")
    def suppliers():
        """Step 5: three supplier catalogs matched to our part master, and the BOM to order."""
        return {
            "accuracy": _run(backend, "SELECT * FROM OEE.V_MATCH_ACCURACY"),
            "offers": _run(backend, """
SELECT PART_NO, SUPPLIER_ID, SUPPLIER_NAME, CATALOG_FORMAT, SUPPLIER_CODE, DESCRIPTION, MATCH_TYPE, EDIT_DIST,
       UNIT_PRICE_INR, LEAD_DAYS, ORDER_MULTIPLE
FROM OEE.V_PART_OFFERS
ORDER BY PART_NO, MATCH_TYPE, UNIT_PRICE_INR"""),
            "bom": _run(backend, """
SELECT PART_NO, DESCRIPTION, URGENT_QTY, REPLENISH_QTY, ORDER_QTY, SUPPLIER_NAME, SUPPLIER_CODE,
       SUPPLIER_DESCRIPTION, UNIT_PRICE_INR, LEAD_DAYS, LINE_TOTAL_INR, PRIORITY
FROM OEE.V_BOM_TO_ORDER
ORDER BY PRIORITY DESC, LINE_TOTAL_INR DESC"""),
        }

    @app.get("/api/pillars")
    def pillars():
        """Evidence for Exasol's five pillars (written by `python -m oee.cli pillars`)."""
        try:
            return _run(backend, """
SELECT PILLAR, METRIC, VALUE_NUM, DETAIL, MEASURED_AT
FROM OEE.PILLAR_EVIDENCE
ORDER BY MEASURED_AT, PILLAR""")
        except Exception:
            return {"rows": [], "sql": "", "ms": 0}

    @app.get("/api/demo/search")
    def demo_search(mode: str):
        """Search one fault's five-stream pattern across the whole history, timed in Exasol."""
        from oee import patterns

        if mode not in patterns.MODES:
            raise HTTPException(400, f"Unknown fault pattern {mode!r}")
        try:
            rows_total = backend.query("SELECT COUNT(*) AS N FROM OEE_SCALE.FEATURES")[0]["N"]
            scale = rows_total > 0
        except Exception:
            scale = False
        if not scale:
            rows_total = backend.query("SELECT COUNT(*) AS N FROM OEE.V_FEATURES")[0]["N"]
        machines = backend.query(f"SELECT COUNT(*) AS N FROM {patterns.source_tables(scale)['machines']}")[0]["N"]
        sql = patterns.search_sql(mode, scale)
        t0 = time.perf_counter()
        backend.execute(sql)
        search_ms = (time.perf_counter() - t0) * 1000.0
        summary = _run(backend, patterns.summary_sql(mode, scale))
        episodes = _run(backend, f"""
SELECT e.MACHINE_ID, e.FIRST_TS, e.LAST_TS, e.READINGS, e.PEAK_Z,
       MIN(s.START_TS) AS FAILED_AT
FROM OEE.PATTERN_EPISODES e
LEFT JOIN {patterns.source_tables(scale)['downtime']} s
  ON  s.MACHINE_ID = e.MACHINE_ID AND s.STOP_TYPE = 'UNPLANNED' AND s.FAILURE_MODE = '{mode}'
  AND s.START_TS >= e.FIRST_TS AND s.START_TS <= ADD_HOURS(e.LAST_TS, {patterns.FOLLOW_HOURS})
GROUP BY e.MACHINE_ID, e.FIRST_TS, e.LAST_TS, e.READINGS, e.PEAK_Z
ORDER BY e.FIRST_TS DESC
LIMIT 6""")
        signature = _run(backend, f"""
SELECT STREAM, STREAM_ORDER, SIGNATURE_Z, LEARNED_FROM_STOPS
FROM OEE.FAULT_SIGNATURES
WHERE FAILURE_MODE = '{mode}'
ORDER BY STREAM_ORDER""")
        s0 = summary["rows"][0] if summary["rows"] else {}
        machine_hours = rows_total / patterns.source_tables(scale)["per_hour"]
        base_rate = (s0.get("FAILURES") or 0) * patterns.FOLLOW_HOURS / machine_hours if machine_hours else None
        return {"mode": mode, "scale": scale, "source": patterns.source_tables(scale)["label"],
                "signature": signature, "base_rate": base_rate,
                "rule": {"alarm_z": patterns.ALARM_Z, "min_hours": patterns.MIN_HOURS, "follow_hours": patterns.FOLLOW_HOURS},
                "rows_scanned": rows_total, "machines": machines,
                "search": {"sql": sql, "ms": round(search_ms, 1)},
                "summary": summary, "episodes": episodes}

    @app.get("/api/demo/replay")
    def demo_replay(mode: str):
        """A held-out failure of this kind, hour by hour for the 48 hours before it."""
        from oee import patterns

        if mode not in patterns.MODES:
            raise HTTPException(400, f"Unknown fault pattern {mode!r}")
        pick_sql, stream_sql = patterns.replay_sql(mode)
        pick = _run(backend, pick_sql)
        if not pick["rows"]:
            return {"mode": mode, "pick": pick, "stream": None, "kit": None}
        # the held-out failures this mode was named for, best first; replay the first one
        # where the same rule as the search selects the fault
        # prefer one where the plant's own store cannot supply the whole kit, so the
        # parts search (step 3) has something to find; the fleet-wide numbers stay in the console
        incomplete = {r["PLANT_ID"] for r in backend.query(f"""
SELECT PLANT_ID FROM OEE.V_SOURCING_BEST
WHERE FAILURE_MODE = '{mode}' AND SOURCE_KIND <> 'LOCAL'""")}
        stream, best = None, None
        for cand in pick["rows"]:
            s = _run(backend, stream_sql.format(machine=cand["MACHINE_ID"], stop_ts=str(cand["START_TS"])[:19]))
            score = (patterns.selection_hour(s["rows"], mode) is not None) * 2 + (cand["PLANT_ID"] in incomplete)
            if best is None or score > best:
                best, p, stream = score, cand, s
            if score == 3:
                break
        pick["rows"] = [p]
        kit = _run(backend, f"""
SELECT PART_NO, QTY, SOURCE_KIND, FROM_LOCATION, ETA_HOURS
FROM OEE.V_SOURCING_BEST
WHERE PLANT_ID = '{p["PLANT_ID"]}' AND FAILURE_MODE = '{mode}'
ORDER BY PART_NO""")
        return {"mode": mode, "pick": pick, "stream": stream, "kit": kit}

    @app.get("/api/demo/parts")
    def demo_parts(mode: str, plant: str):
        """Step 3: search the plant's own store system for the kit, then every other store system."""
        from oee import patterns
        from oee.stores import STORE_SYSTEMS

        if mode not in patterns.MODES:
            raise HTTPException(400, f"Unknown fault pattern {mode!r}")
        if plant not in STORE_SYSTEMS or plant == "HYD":
            raise HTTPException(400, f"Unknown plant {plant!r}")
        local = _run(backend, patterns.local_parts_sql(mode, plant))
        network = _run(backend, patterns.network_parts_sql(mode, plant))
        systems = [{"location": k, "table": v[0], "system": v[1], "part_format": v[3]} for k, v in STORE_SYSTEMS.items()]
        return {"mode": mode, "plant": plant, "own_system": STORE_SYSTEMS[plant][1], "own_table": STORE_SYSTEMS[plant][0],
                "systems": systems, "local": local, "network": network}

    @app.get("/api/demo/quality")
    def demo_quality():
        """Quality check before the shift: the QUALITY_RISK UDF scores every shift, timed in Exasol."""
        from oee.quality import QUALITY_FEATURES

        cols = ", ".join(QUALITY_FEATURES)
        score_sql = f"""CREATE OR REPLACE TABLE OEE.QUALITY_CHECK_RUN AS
SELECT MACHINE_ID, SHIFT_START, OEE.QUALITY_RISK({cols}) AS QUALITY_RISK
FROM OEE.V_QUALITY_TRAINING"""
        t0 = time.perf_counter()
        backend.execute(score_sql)
        score_ms = (time.perf_counter() - t0) * 1000.0
        shifts = backend.query("SELECT COUNT(*) AS N FROM OEE.QUALITY_CHECK_RUN")[0]["N"]
        next_shift = _run(backend, """
SELECT MACHINE_ID, PLANT_ID, MACHINE_TYPE, NEXT_SHIFT_START, TOOL_WEAR_MIN, VIB_AVG_6H, TEMP_DIFF_AVG_6H,
       PREV_SCRAP_RATE, QUALITY_RISK, RECOMMENDATION
FROM OEE.V_QUALITY_NEXT_SHIFT
ORDER BY QUALITY_RISK DESC""")
        # one held-out shift that went bad right after a good one, which the check flagged
        example = _run(backend, """
WITH cutoff AS (SELECT MAX(TRAIN_CUTOFF) AS TS FROM OEE.MODEL_REGISTRY WHERE MODEL_NAME = 'QUALITY_RISK'),
normal AS (
    SELECT m.MACHINE_TYPE,
           AVG(q.TOOL_WEAR_MIN) AS TOOL_MU,    STDDEV(q.TOOL_WEAR_MIN) AS TOOL_SD,
           AVG(q.VIB_AVG_6H) AS VIB_MU,        STDDEV(q.VIB_AVG_6H) AS VIB_SD,
           AVG(q.TEMP_DIFF_AVG_6H) AS TEMP_MU, STDDEV(q.TEMP_DIFF_AVG_6H) AS TEMP_SD
    FROM OEE.QUALITY_SCORES q JOIN OEE.MACHINES m ON m.MACHINE_ID = q.MACHINE_ID
    GROUP BY m.MACHINE_TYPE
)
SELECT q.MACHINE_ID, m.MACHINE_TYPE, q.SHIFT_START, q.SHIFT_END, q.TOTAL_COUNT, q.GOOD_COUNT,
       q.TOTAL_COUNT - q.GOOD_COUNT AS BAD_PARTS,
       ROUND(q.TOTAL_COUNT * 0.025) AS NORMAL_SCRAP,
       q.SCRAP_RATE, q.PREV_SCRAP_RATE, q.QUALITY_RISK,
       q.TOOL_WEAR_MIN, q.VIB_AVG_6H, q.TEMP_DIFF_AVG_6H,
       (q.TOOL_WEAR_MIN - n.TOOL_MU) / NULLIF(n.TOOL_SD, 0)      AS TOOL_Z,
       (q.VIB_AVG_6H - n.VIB_MU) / NULLIF(n.VIB_SD, 0)          AS VIB_Z,
       -(q.TEMP_DIFF_AVG_6H - n.TEMP_MU) / NULLIF(n.TEMP_SD, 0) AS HEAT_Z,
       n.TOOL_MU
FROM OEE.QUALITY_SCORES q
CROSS JOIN cutoff c
JOIN OEE.MACHINES m ON m.MACHINE_ID = q.MACHINE_ID
JOIN normal n ON n.MACHINE_TYPE = m.MACHINE_TYPE
WHERE q.SHIFT_START >= c.TS AND q.LABEL_BAD_SHIFT = 1 AND q.PREV_SCRAP_RATE <= 0.07 AND q.QUALITY_RISK >= 0.5
ORDER BY q.TOTAL_COUNT - q.GOOD_COUNT DESC
LIMIT 1""")
        impact = _run(backend, "SELECT * FROM OEE.QUALITY_IMPACT ORDER BY ORD")
        return {"score": {"sql": score_sql, "ms": round(score_ms, 1)}, "shifts_scored": shifts,
                "next_shift": next_shift, "example": example, "impact": impact}

    @app.get("/api/demo/order")
    def demo_order(part: str, qty: int = 1):
        """Order a part: a normal lookup by our part number, then a deep scan of every supplier file."""
        from oee import patterns

        parts = {r["PART_NO"]: r for r in backend.query("SELECT PART_NO, DESCRIPTION, MATCH_KEY FROM OEE.PART_MASTER")}
        if part not in parts:
            raise HTTPException(400, f"Unknown part {part!r}")
        qty = max(1, min(int(qty), 1000))
        plain = _run(backend, patterns.plain_search_sql(part))
        files = _run(backend, """
SELECT s.SUPPLIER_ID, s.NAME AS SUPPLIER_NAME, s.CATALOG_FORMAT, s.CURRENCY, COUNT(*) AS LINES
FROM OEE.V_SUPPLIER_OFFERS o JOIN OEE.SUPPLIERS s ON s.SUPPLIER_ID = o.SUPPLIER_ID
GROUP BY s.SUPPLIER_ID, s.NAME, s.CATALOG_FORMAT, s.CURRENCY
ORDER BY s.SUPPLIER_ID""")
        deep = _run(backend, patterns.deep_scan_sql(part))
        return {"part": parts[part], "qty": qty, "plain": plain, "files": files, "deep": deep}

    def _demo_window():
        m = backend.query("""SELECT MACHINE_ID FROM OEE.V_MACHINE_STATUS ORDER BY RISK_24H DESC, MACHINE_ID LIMIT 1""")[0]["MACHINE_ID"]
        # the 12-minute window, among those ending in the last 90 minutes of data, with the
        # most gaps in the minute-by-minute streams, so the filling is visible on screen
        t1 = str(backend.query(f"""
WITH last_ts AS (SELECT MAX(READING_TS) AS T FROM OEE.TELEMETRY_FUSED WHERE MACHINE_ID = '{m}'),
gaps AS (
    SELECT f.READING_TS,
           SUM(CASE WHEN f.VIB_Q = 0 OR f.VIB_Q IS NULL THEN 1 ELSE 0 END
               + CASE WHEN f.DRV_Q = 0 OR f.DRV_Q IS NULL THEN 1 ELSE 0 END)
             OVER (ORDER BY f.READING_TS ROWS BETWEEN 11 PRECEDING AND CURRENT ROW) AS GAPS
    FROM OEE.TELEMETRY_FUSED f CROSS JOIN last_ts l
    WHERE f.MACHINE_ID = '{m}' AND f.READING_TS >= ADD_MINUTES(l.T, -110)
)
SELECT g.READING_TS AS T
FROM gaps g CROSS JOIN last_ts l
WHERE g.READING_TS >= ADD_MINUTES(l.T, -90)
ORDER BY g.GAPS DESC, g.READING_TS DESC
LIMIT 1""")[0]["T"])[:19]
        from datetime import datetime, timedelta

        t0 = (datetime.fromisoformat(t1) - timedelta(minutes=11)).strftime("%Y-%m-%d %H:%M:%S")
        return m, t0, t1

    @app.get("/api/demo/sources")
    def demo_sources():
        """Step 0: one machine's last minutes as each of the five source systems holds them."""
        from oee import patterns

        m, t0, t1 = _demo_window()
        q = patterns.source_window_sql(m, t0, t1)
        out = {k: _run(backend, v) for k, v in q.items()}
        as_of = backend.query("SELECT DATA_AS_OF FROM OEE.FUSION_CONFIG")[0]["DATA_AS_OF"]
        raw_rows = backend.query("SELECT SUM(RAW_ROWS) AS N FROM OEE.V_SOURCE_HEALTH")[0]["N"]
        return {"machine": m, "t0": t0, "t1": t1, "data_as_of": as_of, "raw_rows": raw_rows, **out}

    @app.post("/api/demo/combine")
    def demo_combine():
        """Step 0: run the real combine-and-complete SQL for the whole plant (into DEMO_* copies), timed."""
        from oee import patterns
        from oee.sqlrun import split_statements  # noqa: F401  (kept for parity with deploy)

        stmts = patterns.fusion_demo_statements()
        t0_ = time.perf_counter()
        for st in stmts:
            backend.execute(st)
        ms = (time.perf_counter() - t0_) * 1000.0
        minutes = backend.query("SELECT COUNT(*) AS N FROM OEE.DEMO_TELEMETRY_FUSED")[0]["N"]
        quality = backend.query("""SELECT
    SUM(CASE WHEN VIB_Q = 1 THEN 1 ELSE 0 END + CASE WHEN TMP_Q = 1 THEN 1 ELSE 0 END + CASE WHEN DRV_Q = 1 THEN 1 ELSE 0 END
      + CASE WHEN PWR_Q = 1 THEN 1 ELSE 0 END + CASE WHEN TOOL_Q = 1 THEN 1 ELSE 0 END) AS MEASURED,
    SUM(CASE WHEN VIB_Q = 0 THEN 1 ELSE 0 END + CASE WHEN TMP_Q = 0 THEN 1 ELSE 0 END + CASE WHEN DRV_Q = 0 THEN 1 ELSE 0 END
      + CASE WHEN PWR_Q = 0 THEN 1 ELSE 0 END + CASE WHEN TOOL_Q = 0 THEN 1 ELSE 0 END) AS FILLED,
    5 * COUNT(*) AS CELLS
FROM OEE.DEMO_TELEMETRY_FUSED""")[0]
        m, t0, t1 = _demo_window()
        fused = _run(backend, patterns.fused_window_sql(m, t0, t1))
        raw_rows = backend.query("SELECT SUM(RAW_ROWS) AS N FROM OEE.V_SOURCE_HEALTH")[0]["N"]
        return {"combine": {"sql": ";\n\n".join(stmts[1:]), "ms": round(ms, 1)}, "minutes": minutes,
                "raw_rows": raw_rows, "quality": quality, "machine": m, "fused": fused}

    @app.get("/demo")
    def demo_page():
        return FileResponse(STATIC / "demo.html")

    @app.get("/api/bench")
    def bench():
        """Latest fleet-scale benchmark run (written by `python -m oee.cli bench`)."""
        try:
            return _run(backend, """
SELECT RUN_ID, RUN_AT, MACHINES, DAYS, CADENCE_MIN, STAGE_ORDER, STAGE, ROWS_IN, SECONDS, ROWS_PER_S, DETAIL
FROM OEE.BENCH_RUNS
WHERE RUN_ID IN (SELECT RUN_ID FROM OEE.BENCH_RUNS WHERE RUN_AT = (SELECT MAX(RUN_AT) FROM OEE.BENCH_RUNS))
ORDER BY STAGE_ORDER""")
        except Exception:
            return {"rows": [], "sql": "", "ms": 0}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.middleware("http")
    async def no_stale_pages(request, call_next):
        resp = await call_next(request)
        if not request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"     # always the latest page and script
        return resp

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def serve(host: str = "127.0.0.1", port: int = 8000):
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port)
