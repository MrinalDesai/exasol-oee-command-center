"""Sensor-fusion benchmark at fleet scale.

Generates one-minute sensor readings for a large fleet (default 100 machines x
100 days, about 14 million readings, ~1 GB of CSV), loads them into Exasol and
times every stage of the pipeline inside the database:

  1. load                  IMPORT of the raw sensor CSV
  2. sensor fusion          rolling 6-hour windows across all sensors, per machine
  3. pattern detection      every reading scored by the in-database model
                            (OEE.FAILURE_RISK_BATCH, a numpy SET UDF run in
                            parallel across machine-day groups)
  4. alerts                 latest risk per machine, machines above threshold
  5. validation             how often an alert was followed by a real stop

The model is the one trained on the demo plant (hourly data). The fusion
windows are defined in time (6 hours = 360 one-minute readings), so the same
features mean the same thing at both cadences.

Timings are printed and stored in OEE.BENCH_RUNS, which the console shows.

    python -m oee.cli bench                        # 100 machines x 100 days
    python -m oee.cli bench --machines 20 --days 30   # quick check first
"""
from __future__ import annotations

import json
import math
import time
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

from oee.features import FEATURES

SCHEMA = "OEE_SCALE"
WINDOW_HOURS = 6


# ---------------------------------------------------------------- data ----

def _upsample(tel: pd.DataFrame, cadence_min: int, rng: np.random.Generator) -> pd.DataFrame:
    """Hourly physics -> readings every `cadence_min` minutes.

    Slow-moving state (tool wear, vibration drift, temperatures) is interpolated
    towards the next hour's value; fast sensor noise is added per reading.
    """
    tel = tel.sort_values(["machine_id", "reading_ts"]).reset_index(drop=True)
    k = 60 // cadence_min
    nxt = tel.groupby("machine_id", sort=False).shift(-1)
    contiguous = (nxt["reading_ts"] - tel["reading_ts"]) == pd.Timedelta(hours=1)
    cols = ["air_temp_k", "process_temp_k", "rpm", "torque_nm", "tool_wear_min", "vibration_mm_s", "power_kw"]
    cur = tel[cols].to_numpy(dtype=np.float64)
    nx = np.where(contiguous.to_numpy()[:, None], nxt[cols].to_numpy(dtype=np.float64), cur)

    n = len(tel)
    frac = np.tile(np.arange(k) / k, n)                       # position inside the hour
    base = np.repeat(cur, k, axis=0) + (np.repeat(nx - cur, k, axis=0) * frac[:, None])
    air, proc, rpm, torque, wear, vib, power = base.T
    m = len(frac)
    air = air + rng.normal(0, 0.15, m)
    proc = proc + rng.normal(0, 0.15, m)
    rpm_n = rpm * (1 + rng.normal(0, 0.01, m))
    torque_n = torque * (1 + rng.normal(0, 0.03, m))
    vib = vib + np.abs(rng.normal(0, 0.08, m))
    power = power * (rpm_n / np.maximum(rpm, 1e-9)) * (torque_n / np.maximum(torque, 1e-9))

    ts = np.repeat(tel["reading_ts"].to_numpy(), k) + np.tile(np.arange(k) * cadence_min, n).astype("timedelta64[m]")
    return pd.DataFrame({
        "machine_id": np.repeat(tel["machine_id"].to_numpy(), k),
        "reading_ts": ts,
        "air_temp_k": air.round(2), "process_temp_k": proc.round(2), "rpm": rpm_n.round(1),
        "torque_nm": torque_n.round(2), "tool_wear_min": wear.round(1), "vibration_mm_s": vib.round(3),
        "power_kw": power.round(3),
    })


def _write_part(args) -> int:
    part, path, cadence_min, seed = args
    up = _upsample(part, cadence_min, np.random.default_rng(seed))
    # "\n" explicitly: on Windows pandas would write "\r\n", which Exasol's IMPORT rejects
    up.to_csv(path, index=False, header=False, date_format="%Y-%m-%d %H:%M:%S", lineterminator="\n")
    return len(up)


def generate_scale(out_dir: Path, machines: int = 100, days: int = 100, cadence_min: int = 1,
                   seed: int = 7, chunk_machines: int = 5, workers: int | None = None) -> dict:
    """Write MACHINES / DOWNTIME CSVs and one TELEMETRY CSV per group of machines
    (written in parallel across CPU cores). Returns sizes."""
    import os
    from concurrent.futures import ProcessPoolExecutor

    from oee.generate import generate

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("telemetry_*.csv"):
        old.unlink()
    per_plant = max(1, math.ceil(machines / 3))
    tables = generate(days=days, seed=seed, per_plant=per_plant, with_production=False)
    mach = tables["MACHINES"].head(machines)
    keep = set(mach.machine_id)
    tel = tables["TELEMETRY"][tables["TELEMETRY"].machine_id.isin(keep)]
    down = tables["DOWNTIME"]
    if down.empty:   # very short runs can have no stops at all
        down = pd.DataFrame(columns=["stop_id", "machine_id", "start_ts", "end_ts", "stop_type", "failure_mode", "part_replaced"])
    down = down[down.machine_id.isin(keep)]
    mach.to_csv(out_dir / "machines.csv", index=False, lineterminator="\n")
    down.to_csv(out_dir / "downtime.csv", index=False, date_format="%Y-%m-%d %H:%M:%S", lineterminator="\n")

    ids = sorted(keep)
    # Windows caps process pools at 61 workers; never start more workers than jobs
    workers = max(1, min(workers or (os.cpu_count() or 2) - 1, 61))
    chunk_machines = max(1, min(chunk_machines, math.ceil(len(ids) / workers)))
    jobs = []
    for j, i in enumerate(range(0, len(ids), chunk_machines)):
        part = tel[tel.machine_id.isin(ids[i:i + chunk_machines])]
        jobs.append((part, out_dir / f"telemetry_{j:03d}.csv", cadence_min, seed + 1 + j))
    workers = min(workers, len(jobs))
    if workers == 1:
        rows = sum(map(_write_part, jobs))
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            rows = sum(ex.map(_write_part, jobs))
    files = sorted(out_dir.glob("telemetry_*.csv"))
    return {"machines": len(mach), "rows": rows, "stops": len(down), "files": len(files),
            "bytes": sum(f.stat().st_size for f in files)}


# ----------------------------------------------------------------- SQL ----

def _schema_sql() -> list[str]:
    return [
        f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
        f"""CREATE OR REPLACE TABLE {SCHEMA}.MACHINES (
    MACHINE_ID VARCHAR(32), PLANT_ID VARCHAR(8), LINE_ID VARCHAR(16), MACHINE_TYPE VARCHAR(16),
    INSTALL_YEAR DECIMAL(4,0), IDEAL_CYCLE_S DOUBLE, NOMINAL_RPM DOUBLE, NOMINAL_TORQUE DOUBLE)""",
        f"""CREATE OR REPLACE TABLE {SCHEMA}.DOWNTIME (
    STOP_ID DECIMAL(12,0), MACHINE_ID VARCHAR(32), START_TS TIMESTAMP, END_TS TIMESTAMP,
    STOP_TYPE VARCHAR(16), FAILURE_MODE VARCHAR(32), PART_REPLACED VARCHAR(32))""",
        f"""CREATE OR REPLACE TABLE {SCHEMA}.TELEMETRY (
    MACHINE_ID VARCHAR(32), READING_TS TIMESTAMP, AIR_TEMP_K DOUBLE, PROCESS_TEMP_K DOUBLE, RPM DOUBLE,
    TORQUE_NM DOUBLE, TOOL_WEAR_MIN DOUBLE, VIBRATION_MM_S DOUBLE, POWER_KW DOUBLE)""",
        """CREATE TABLE IF NOT EXISTS OEE.BENCH_RUNS (
    RUN_ID VARCHAR(40), RUN_AT TIMESTAMP, MACHINES DECIMAL(8,0), DAYS DECIMAL(6,0), CADENCE_MIN DECIMAL(4,0),
    STAGE_ORDER DECIMAL(3,0), STAGE VARCHAR(40), ROWS_IN DECIMAL(14,0), SECONDS DOUBLE, ROWS_PER_S DOUBLE,
    DETAIL VARCHAR(2000))""",
    ]


def fusion_sql(cadence_min: int) -> str:
    w = WINDOW_HOURS * 60 // cadence_min                     # readings in 6 hours
    win = f"PARTITION BY t.MACHINE_ID ORDER BY t.READING_TS ROWS BETWEEN {w - 1} PRECEDING AND CURRENT ROW"
    return f"""CREATE OR REPLACE TABLE {SCHEMA}.FEATURES AS
SELECT
    t.MACHINE_ID,
    t.READING_TS,
    t.AIR_TEMP_K,
    t.PROCESS_TEMP_K,
    t.PROCESS_TEMP_K - t.AIR_TEMP_K                                  AS TEMP_DIFF_K,
    t.RPM,
    t.TORQUE_NM,
    t.TORQUE_NM / m.NOMINAL_TORQUE                                   AS TORQUE_RATIO,
    t.TOOL_WEAR_MIN,
    t.TOOL_WEAR_MIN * t.TORQUE_NM / m.NOMINAL_TORQUE                 AS WEAR_X_LOAD,
    t.VIBRATION_MM_S,
    t.POWER_KW,
    AVG(t.VIBRATION_MM_S) OVER ({win})                              AS VIB_AVG_6H,
    t.VIBRATION_MM_S - COALESCE(
        LAG(t.VIBRATION_MM_S, {w}) OVER (PARTITION BY t.MACHINE_ID ORDER BY t.READING_TS),
        t.VIBRATION_MM_S)                                            AS VIB_DELTA_6H,
    AVG(t.PROCESS_TEMP_K - t.AIR_TEMP_K) OVER ({win})               AS TEMP_DIFF_AVG_6H,
    AVG(t.POWER_KW) OVER ({win})                                    AS POWER_AVG_6H,
    CASE WHEN m.MACHINE_TYPE = 'CNC_MILL'  THEN 1 ELSE 0 END         AS IS_MILL,
    CASE WHEN m.MACHINE_TYPE = 'CNC_LATHE' THEN 1 ELSE 0 END         AS IS_LATHE,
    CASE WHEN m.MACHINE_TYPE = 'PRESS'     THEN 1 ELSE 0 END         AS IS_PRESS,
    CASE WHEN m.MACHINE_TYPE = 'PACKER'    THEN 1 ELSE 0 END         AS IS_PACKER
FROM {SCHEMA}.TELEMETRY t
JOIN {SCHEMA}.MACHINES m ON m.MACHINE_ID = t.MACHINE_ID"""


# Readings are scored independently, so the work is split into machine-day groups:
# many more groups than CPU cores lets Exasol keep every core busy.
SCORING_GROUPS = "MACHINE_ID, CAST(READING_TS AS DATE)"


def scoring_sql() -> str:
    return f"""CREATE OR REPLACE TABLE {SCHEMA}.SCORES AS
SELECT OEE.FAILURE_RISK_BATCH(MACHINE_ID, READING_TS, {", ".join(FEATURES)})
FROM {SCHEMA}.FEATURES
GROUP BY {SCORING_GROUPS}"""


ALERTS_SQL = f"""WITH latest AS (
    SELECT MACHINE_ID, MAX(READING_TS) AS TS FROM {SCHEMA}.SCORES GROUP BY MACHINE_ID
)
SELECT COUNT(*)                                             AS MACHINES,
       SUM(CASE WHEN s.RISK_24H >= 0.5 THEN 1 ELSE 0 END)   AS CRITICAL,
       SUM(CASE WHEN s.RISK_24H >= 0.1 THEN 1 ELSE 0 END)   AS WATCH_OR_WORSE,
       MAX(s.RISK_24H)                                      AS MAX_RISK
FROM {SCHEMA}.SCORES s
JOIN latest l ON l.MACHINE_ID = s.MACHINE_ID AND l.TS = s.READING_TS"""

VALIDATION_SQL = f"""WITH followed AS (
    SELECT DISTINCT s.MACHINE_ID, s.READING_TS
    FROM {SCHEMA}.SCORES s
    JOIN {SCHEMA}.DOWNTIME d
      ON  d.MACHINE_ID = s.MACHINE_ID
      AND d.STOP_TYPE  = 'UNPLANNED'
      AND d.START_TS   >  s.READING_TS
      AND d.START_TS   <= ADD_HOURS(s.READING_TS, 24)
)
SELECT COUNT(*)                                                          AS READINGS,
       SUM(CASE WHEN f.MACHINE_ID IS NOT NULL THEN 1 ELSE 0 END)         AS FOLLOWED_BY_STOP,
       SUM(CASE WHEN s.RISK_24H >= 0.5 THEN 1 ELSE 0 END)                AS ALERTS,
       SUM(CASE WHEN s.RISK_24H >= 0.5 AND f.MACHINE_ID IS NOT NULL THEN 1 ELSE 0 END) AS ALERTS_FOLLOWED
FROM {SCHEMA}.SCORES s
LEFT JOIN followed f ON f.MACHINE_ID = s.MACHINE_ID AND f.READING_TS = s.READING_TS"""


# ----------------------------------------------------------- benchmark ----

class _ChainedFiles:
    """Read several CSV files as one stream, so the load is a single IMPORT."""

    def __init__(self, paths):
        self._paths = list(paths)
        self._fh = None

    def read(self, size=-1):
        while True:
            if self._fh is None:
                if not self._paths:
                    return b""
                self._fh = open(self._paths.pop(0), "rb")
            chunk = self._fh.read(size)
            if chunk:
                return chunk
            self._fh.close()
            self._fh = None


def _probe_parallelism(conn, groups: int = 16, seconds: float = 1.0) -> dict:
    """How many SET-UDF instances Exasol runs at once: every group sleeps `seconds`."""
    conn.execute(f"""CREATE OR REPLACE PYTHON3 SET SCRIPT OEE.PARALLEL_PROBE(G DECIMAL(6,0))
EMITS (G DECIMAL(6,0), PID DECIMAL(12,0), CPUS DECIMAL(6,0), T0 DOUBLE, T1 DOUBLE) AS
import os, time
def run(ctx):
    t0 = time.time()
    time.sleep({seconds})
    try:
        cpus = len(os.sched_getaffinity(0))
    except Exception:
        cpus = os.cpu_count() or 0
    ctx.emit(ctx.G, os.getpid(), cpus, t0, time.time())
/""")
    t0 = time.perf_counter()
    rows = conn.execute(f"""SELECT OEE.PARALLEL_PROBE(G) FROM
        (SELECT LEVEL AS G FROM DUAL CONNECT BY LEVEL <= {groups}) GROUP BY G""").fetchall()
    wall = time.perf_counter() - t0
    starts = sorted(float(r[3]) for r in rows)
    ends = sorted(float(r[4]) for r in rows)
    # peak number of instances alive at the same moment
    events = [(s, 1) for s in starts] + [(e, -1) for e in ends]
    peak = cur = 0
    for _, d in sorted(events):
        cur += d
        peak = max(peak, cur)
    return {"groups": groups, "wall_s": round(wall, 2), "peak_parallel": peak,
            "processes": len({r[1] for r in rows}), "cpus_seen_by_udf": int(rows[0][2]) if rows else None}


def run_bench(conn, data_dir: Path, machines: int, days: int, cadence_min: int, model_path: Path,
              regenerate: bool = True, log=print) -> list[dict]:
    import xgboost as xgb

    from oee.compile_udf import booster_to_spec, render_batch_udf
    from oee.sqlrun import split_statements

    run_id = uuid.uuid4().hex[:12]
    results: list[dict] = []

    def stage(order: int, name: str, rows: int | None, seconds: float, detail: dict, sql: str | None = None):
        rps = (rows / seconds) if rows and seconds > 0 else None
        results.append({"order": order, "stage": name, "rows": rows, "seconds": seconds, "rows_per_s": rps,
                        "detail": detail, "sql": sql})
        rate = f"{rps:>14,.0f} rows/s" if rps else " " * 21
        log(f"  {order}. {name:<22} {seconds:>8.2f} s  {rate}  {json.dumps(detail)}")

    if regenerate or not list(data_dir.glob("telemetry_*.csv")):
        log(f"Generating {machines} machines x {days} days at {cadence_min}-minute readings (on this PC, not timed as Exasol)...")
        t0 = time.perf_counter()
        info = generate_scale(data_dir, machines=machines, days=days, cadence_min=cadence_min)
        log(f"  generated {info['rows']:,} readings in {info['files']} files, {info['bytes'] / 1e9:.2f} GB, in {time.perf_counter() - t0:.0f} s")
    files = sorted(data_dir.glob("telemetry_*.csv"))

    log("Preparing schema and model...")
    for s in _schema_sql():
        conn.execute(s)
    mach = pd.read_csv(data_dir / "machines.csv")
    mach.columns = [c.upper() for c in mach.columns]
    conn.import_from_pandas(mach, (SCHEMA, "MACHINES"))
    down = pd.read_csv(data_dir / "downtime.csv")
    down.columns = [c.upper() for c in down.columns]
    conn.import_from_pandas(down, (SCHEMA, "DOWNTIME"))
    booster = xgb.Booster()
    booster.load_model(str(model_path))
    conn.execute(split_statements(render_batch_udf(booster_to_spec(booster)))[0])

    probe = _probe_parallelism(conn)
    log(f"  UDF parallelism check: {probe['groups']} one-second groups took {probe['wall_s']} s; "
        f"peak {probe['peak_parallel']} instances at once; UDF sees {probe['cpus_seen_by_udf']} CPUs")

    log("Running the pipeline in Exasol:")
    # 1. load
    # one IMPORT for all files; no client-side gzip (it only costs CPU over localhost)
    from oee.db import connect

    # The HTTPS transfer can occasionally drop at the very end of the stream; retry cleanly.
    attempts = 0
    while True:
        attempts += 1
        conn.execute(f"TRUNCATE TABLE {SCHEMA}.TELEMETRY")
        t0 = time.perf_counter()
        try:
            with connect(compression=False) as loader:
                loader.import_from_file(_ChainedFiles(files), (SCHEMA, "TELEMETRY"))
            secs = time.perf_counter() - t0
            break
        except Exception as exc:  # pyexasol wraps transport errors in ExaImportError
            if attempts >= 3:
                raise
            log(f"  load attempt {attempts} failed ({str(exc).strip().splitlines()[-1][:120]}); retrying")
    n = conn.execute(f"SELECT COUNT(*) FROM {SCHEMA}.TELEMETRY").fetchval()
    size_gb = sum(f.stat().st_size for f in files) / 1e9
    stage(1, "load", n, secs, {"gb": round(size_gb, 2), "files": len(files), "attempts": attempts},
          f"IMPORT INTO {SCHEMA}.TELEMETRY FROM CSV  -- {len(files)} files streamed as one IMPORT by pyexasol")

    # 2. sensor fusion
    sql = fusion_sql(cadence_min)
    t0 = time.perf_counter()
    conn.execute(sql)
    stage(2, "sensor fusion", n, time.perf_counter() - t0,
          {"signals": 7, "window": f"{WINDOW_HOURS} h", "features": len(FEATURES)}, sql)

    # 3. pattern detection
    sql = scoring_sql()
    t0 = time.perf_counter()
    conn.execute(sql)
    secs = time.perf_counter() - t0
    scored = conn.execute(f"SELECT COUNT(*) FROM {SCHEMA}.SCORES").fetchval()
    stage(3, "pattern detection", scored, secs, {"model": f"{len(booster.get_dump())} trees", "udf": "OEE.FAILURE_RISK_BATCH",
                                                  "groups": "machine-day"}, sql)

    # where did the scoring time go? (diagnostic, not part of the timed pipeline)
    conn.execute(split_statements(render_batch_udf(booster_to_spec(booster), name="OEE.FAILURE_RISK_TIMING", timing=True))[0])
    wall_t0 = time.time()
    t0 = time.perf_counter()
    rows = conn.execute(f"""SELECT OEE.FAILURE_RISK_TIMING(MACHINE_ID, READING_TS, {", ".join(FEATURES)})
        FROM {SCHEMA}.FEATURES GROUP BY {SCORING_GROUPS}""").fetchall()
    wall = time.perf_counter() - t0
    if rows:
        # columns: MACHINE_ID, N_ROWS, T_MODULE, T_LOAD, T_START, T_FETCH, T_SCORE, T_EMIT, T_END, VM_PID
        n_tot = sum(int(r[1]) for r in rows)
        vms = {int(r[9]) for r in rows}
        fetch = sum(float(r[5]) for r in rows)
        sc = sum(float(r[6]) for r in rows)
        load_s = max(float(r[3]) for r in rows)
        first_start = min(float(r[2]) for r in rows) - wall_t0
        busy = fetch + sc
        log(f"  scoring breakdown: {len(rows)} groups on {len(vms)} parallel instances, wall {wall:.1f} s; "
            f"first instance started after {first_start:.1f} s; model load {load_s:.2f} s per instance; "
            f"fetch {fetch:.1f} s + score {sc:.1f} s summed over instances "
            f"({busy / max(wall, 1e-9):.0f} cores busy on average); scoring {n_tot / max(sc, 1e-9):,.0f} rows/s per core")

    # 4. alerts
    t0 = time.perf_counter()
    a = conn.execute(ALERTS_SQL).fetchone()
    stage(4, "alerts", scored, time.perf_counter() - t0,
          {"machines": int(a[0]), "critical": int(a[1] or 0), "watch_or_worse": int(a[2] or 0)}, ALERTS_SQL)

    # 5. validation
    t0 = time.perf_counter()
    v = conn.execute(VALIDATION_SQL).fetchone()
    secs = time.perf_counter() - t0
    readings, followed, alerts, hits = (int(x or 0) for x in v)
    base = followed / readings if readings else 0
    prec = hits / alerts if alerts else 0
    stage(5, "validation", readings, secs,
          {"alert_precision": round(prec, 3), "base_rate": round(base, 3), "lift": round(prec / base, 1) if base else None},
          VALIDATION_SQL)

    total_exasol = sum(r["seconds"] for r in results)
    log(f"Total time in Exasol: {total_exasol:.1f} s for {n:,} readings")

    for r in results:
        conn.execute(
            "INSERT INTO OEE.BENCH_RUNS VALUES ({run!s}, CURRENT_TIMESTAMP, {m!d}, {d!d}, {c!d}, {o!d}, {st!s}, {rows!d}, {sec!f}, {rps!f}, {det!s})",
            {"run": run_id, "m": str(machines), "d": str(days), "c": str(cadence_min), "o": str(r["order"]),
             "st": r["stage"], "rows": str(int(r["rows"] or 0)), "sec": f"{float(r['seconds']):.6f}",
             "rps": f"{float(r['rows_per_s'] or 0.0):.3f}", "det": json.dumps(r["detail"])[:2000]},
        )
    return results
