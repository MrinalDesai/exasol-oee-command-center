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

from oee.assess import MODE_PARTS, MODES, assess
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


# Latest reading per machine, each signal compared with its machine type's normal.
def _fleet_sql(machine: str | None = None) -> str:
    where = f"WHERE r.MACHINE_ID = '{machine}'" if machine else ""
    return f"""
WITH base AS (
    SELECT MACHINE_TYPE,
        AVG(TOOL_WEAR_MIN)  AS TW_BASE,  STDDEV(TOOL_WEAR_MIN)  AS TW_SD,
        AVG(VIBRATION_MM_S) AS VIB_BASE, STDDEV(VIBRATION_MM_S) AS VIB_SD,
        AVG(TEMP_DIFF_K)    AS TD_BASE,  STDDEV(TEMP_DIFF_K)    AS TD_SD,
        AVG(POWER_KW)       AS PW_BASE,  STDDEV(POWER_KW)       AS PW_SD,
        AVG(TORQUE_RATIO)   AS TR_BASE,  STDDEV(TORQUE_RATIO)   AS TR_SD
    FROM OEE.RISK_SCORES
    GROUP BY MACHINE_TYPE
)
SELECT r.MACHINE_ID, r.PLANT_ID, m.LINE_ID, r.MACHINE_TYPE, r.READING_TS, r.RISK_24H,
       r.MTBF_HOURS, r.MTTR_HOURS, r.FAILURES,
       r.TOOL_WEAR_MIN, r.VIBRATION_MM_S, r.TEMP_DIFF_K, r.POWER_KW, r.TORQUE_RATIO,
       b.TW_BASE, b.VIB_BASE, b.TD_BASE, b.PW_BASE, b.TR_BASE,
       (r.TOOL_WEAR_MIN  - b.TW_BASE)  / NULLIF(b.TW_SD, 0)  AS TW_Z,
       (r.VIBRATION_MM_S - b.VIB_BASE) / NULLIF(b.VIB_SD, 0) AS VIB_Z,
       (r.TEMP_DIFF_K    - b.TD_BASE)  / NULLIF(b.TD_SD, 0)  AS TD_Z,
       (r.POWER_KW       - b.PW_BASE)  / NULLIF(b.PW_SD, 0)  AS PW_Z,
       (r.TORQUE_RATIO   - b.TR_BASE)  / NULLIF(b.TR_SD, 0)  AS TR_Z
FROM OEE.V_RISK_LATEST r
JOIN OEE.MACHINES m ON m.MACHINE_ID = r.MACHINE_ID
JOIN base b ON b.MACHINE_TYPE = r.MACHINE_TYPE
{where}
ORDER BY m.LINE_ID, r.MACHINE_ID"""


_MODE_SQL = """
SELECT m.MACHINE_TYPE, d.FAILURE_MODE,
       COUNT(*)                                               AS STOPS,
       AVG(SECONDS_BETWEEN(d.END_TS, d.START_TS) / 3600.0)    AS AVG_HOURS
FROM OEE.DOWNTIME d
JOIN OEE.MACHINES m ON m.MACHINE_ID = d.MACHINE_ID
WHERE d.STOP_TYPE = 'UNPLANNED'
GROUP BY m.MACHINE_TYPE, d.FAILURE_MODE"""

_PLANNED_SQL = """
SELECT AVG(SECONDS_BETWEEN(END_TS, START_TS) / 3600.0) AS PLANNED_HOURS
FROM OEE.DOWNTIME
WHERE STOP_TYPE = 'PLANNED'"""

_PARTS_SQL = """
SELECT PLANT_ID, PART_NO, ON_HAND, LEAD_TIME_DAYS
FROM OEE.SPARE_PARTS"""

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
        with self._lock:
            stmt = self._connection().execute(sql)
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
ORDER BY DEPLOYED_AT DESC
LIMIT 1""")

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
        fleet = _run(backend, _fleet_sql(machine))
        modes = _run(backend, _MODE_SQL)
        planned = _run(backend, _PLANNED_SQL)
        parts_q = _run(backend, _PARTS_SQL)
        wos = _run(backend, _WO_SQL)
        planned_h = planned["rows"][0]["PLANNED_HOURS"] if planned["rows"] else None
        machines = assess(fleet["rows"], modes["rows"], planned_h, parts_q["rows"], wos["rows"])
        meta = [{"sql": q["sql"], "ms": q["ms"]} for q in (fleet, modes, planned, parts_q, wos)]
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
        part_list = ", ".join(f"'{MODE_PARTS[x['mode']]}'" for x in a["likely_modes"][:3])
        parts = _run(backend, f"""
SELECT PART_NO, ON_HAND, REORDER_POINT, LEAD_TIME_DAYS, UNIT_COST_INR
FROM OEE.SPARE_PARTS
WHERE PLANT_ID = '{a["plant_id"]}' AND PART_NO IN ({part_list})""")
        order = [MODE_PARTS[x["mode"]] for x in a["likely_modes"][:3]]
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
        top = a["likely_modes"][0]["mode"]
        next_id = _run(backend, "SELECT COALESCE(MAX(WO_ID), 0) + 1 AS NEXT_ID FROM OEE.WORK_ORDERS")["rows"][0]["NEXT_ID"]
        sql = f"""
INSERT INTO OEE.WORK_ORDERS (WO_ID, MACHINE_ID, PLANT_ID, FAILURE_MODE, PART_NO, RISK_AT_OPEN, OPENED_AT, STATUS)
VALUES ({int(next_id)}, '{m}', '{a["plant_id"]}', '{top}', '{MODE_PARTS[top]}', {float(a["risk"]):.6f}, CURRENT_TIMESTAMP, 'OPEN')""".strip()
        t0 = time.perf_counter()
        backend.execute(sql)
        ms = (time.perf_counter() - t0) * 1000.0
        return {"wo_id": int(next_id), "sql": sql, "ms": round(ms, 1)}

    @app.post("/api/workorders/{wo_id}/close")
    def close_workorder(wo_id: int):
        sql = f"UPDATE OEE.WORK_ORDERS SET STATUS = 'CLOSED' WHERE WO_ID = {int(wo_id)}"
        t0 = time.perf_counter()
        backend.execute(sql)
        return {"sql": sql, "ms": round((time.perf_counter() - t0) * 1000.0, 1)}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def serve(host: str = "127.0.0.1", port: int = 8000):
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port)
