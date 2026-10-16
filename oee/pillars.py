"""Evidence for Exasol's five pillars, measured on this database.

    python -m oee.cli pillars [--sessions 1,4,8,16] [--seconds 8]

  1. in-memory MPP          from the bench (UDF instances in parallel); this just reads it
  2. columnar compression   raw vs in-memory size of every OEE table (EXA_ALL_OBJECT_SIZES)
  3. self-tuning            indexes Exasol created by itself (we never create one)
  4. high concurrency       N console sessions running the five-step query mix at once
  5. in-database AI         the two model UDFs and the SQL steps that call them

Results go to OEE.PILLAR_EVIDENCE, which the console shows.
"""
from __future__ import annotations

import json
import statistics
import threading
import time

# What one console user does: open a machine and look at the five steps
QUERY_MIX = [
    "SELECT * FROM OEE.V_SOURCE_HEALTH",
    "SELECT * FROM OEE.V_MACHINE_STATUS ORDER BY RISK_24H DESC",
    "SELECT * FROM OEE.V_ROOT_CAUSE_LATEST WHERE CAUSE_RANK <= 3",
    "SELECT * FROM OEE.V_STREAM_PATTERNS_LATEST",
    "SELECT * FROM OEE.V_MACHINE_SOURCING",
    "SELECT * FROM OEE.V_QUALITY_NEXT_SHIFT",
    "SELECT * FROM OEE.V_BOM_TO_ORDER",
    "SELECT PLANT_ID, AVG(OEE) FROM OEE.V_OEE_DAILY GROUP BY PLANT_ID",
]

DDL = """CREATE OR REPLACE TABLE OEE.PILLAR_EVIDENCE (
    PILLAR      VARCHAR(40),
    METRIC      VARCHAR(80),
    VALUE_NUM   DOUBLE,
    DETAIL      VARCHAR(2000000),
    MEASURED_AT TIMESTAMP
)"""


def _concurrency(sessions: int, seconds: float, connect) -> dict:
    lat: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()
    stop = time.perf_counter() + seconds

    def worker(k: int):
        with connect() as c:
            i = k
            while time.perf_counter() < stop:
                q = QUERY_MIX[i % len(QUERY_MIX)]
                t0 = time.perf_counter()
                try:
                    c.execute(q).fetchall()
                except Exception as exc:  # noqa: BLE001
                    with lock:
                        errors.append(str(exc)[:120])
                    return
                with lock:
                    lat.append(time.perf_counter() - t0)
                i += 1

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(sessions)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0
    lat.sort()
    p95 = lat[int(len(lat) * 0.95) - 1] if lat else None
    return {"sessions": sessions, "queries": len(lat), "qps": len(lat) / wall if wall else 0,
            "p50_ms": statistics.median(lat) * 1000 if lat else None,
            "p95_ms": p95 * 1000 if p95 else None, "errors": errors[:3]}


def run_pillars(conn, connect, sessions=(1, 4, 8, 16), seconds=8.0, log=print) -> list[tuple]:
    rows = []

    def add(pillar, metric, value, detail=""):
        rows.append((pillar, metric, value, detail if isinstance(detail, str) else json.dumps(detail)))
        log(f"  {pillar:22s} {metric:44s} {value if value is not None else '—'}")

    # 2. columnar storage and compression
    try:
        conn.execute("FLUSH STATISTICS")
    except Exception:  # noqa: BLE001
        pass
    sizes = conn.execute("""
        SELECT OBJECT_NAME, RAW_OBJECT_SIZE, MEM_OBJECT_SIZE
        FROM EXA_ALL_OBJECT_SIZES
        WHERE ROOT_NAME = 'OEE' AND OBJECT_TYPE = 'TABLE'
        ORDER BY RAW_OBJECT_SIZE DESC""").fetchall()
    raw = sum(float(r[1] or 0) for r in sizes)
    mem = sum(float(r[2] or 0) for r in sizes)
    add("columnar compression", "raw / in-memory size, all OEE tables", raw / mem if mem else None,
        [{"table": r[0], "raw_mb": round(float(r[1] or 0) / 2**20, 2), "mem_mb": round(float(r[2] or 0) / 2**20, 2)}
         for r in sizes[:12]])
    add("columnar compression", "raw MB", raw / 2**20)
    add("columnar compression", "in-memory MB", mem / 2**20)

    # 3. self-tuning: indexes Exasol built for our joins on its own
    try:
        idx = conn.execute("""
            SELECT INDEX_TABLE, COUNT(*) FROM EXA_DBA_INDICES
            WHERE INDEX_SCHEMA = 'OEE' GROUP BY INDEX_TABLE ORDER BY 2 DESC""").fetchall()
        add("self-tuning", "indexes created automatically", sum(int(r[1]) for r in idx),
            [{"table": r[0], "indexes": int(r[1])} for r in idx])
    except Exception as exc:  # noqa: BLE001
        add("self-tuning", "indexes created automatically", None, str(exc)[:200])
    add("self-tuning", "indexes, partitions or hints we wrote", 0, "none in sql/*.sql")

    # 4. high concurrency
    for n in sessions:
        r = _concurrency(n, seconds, connect)
        add("high concurrency", f"{n} sessions · queries/s", r["qps"], r)
        log(f"      p50 {r['p50_ms'] or 0:.0f} ms · p95 {r['p95_ms'] or 0:.0f} ms · {r['queries']} queries"
            + (f" · errors: {r['errors']}" if r["errors"] else ""))

    # 5. in-database AI
    udfs = conn.execute("""
        SELECT SCRIPT_NAME FROM EXA_ALL_SCRIPTS WHERE SCRIPT_SCHEMA = 'OEE' ORDER BY 1""").fetchall()
    add("in-database AI", "Python UDFs in the OEE schema", len(udfs), [u[0] for u in udfs])
    scored = conn.execute("""SELECT (SELECT COUNT(*) FROM OEE.RISK_SCORES) + (SELECT COUNT(*) FROM OEE.RISK_SCORES_NAIVE)
                                    + (SELECT COUNT(*) FROM OEE.QUALITY_SCORES)""").fetchval()
    add("in-database AI", "rows scored in-database by model UDFs", float(scored))

    # 1. in-memory MPP: from the latest bench run, if any
    try:
        r = conn.execute("""
            SELECT ROWS_PER_S, DETAIL FROM OEE.BENCH_RUNS
            WHERE STAGE = 'pattern detection'
            AND RUN_AT = (SELECT MAX(RUN_AT) FROM OEE.BENCH_RUNS)""").fetchone()
        if r:
            add("in-memory MPP", "pattern detection rows/s (bench)", float(r[0]), r[1] or "")
    except Exception:  # noqa: BLE001
        log("  in-memory MPP          run `python -m oee.cli bench` for this one")

    conn.execute(DDL)
    for pillar, metric, value, detail in rows:
        conn.execute("INSERT INTO OEE.PILLAR_EVIDENCE VALUES ({p}, {m}, {v}, {d}, CURRENT_TIMESTAMP)",
                     {"p": pillar, "m": metric, "v": value, "d": detail})
    return rows
