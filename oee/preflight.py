"""Check that this Exasol supports every SQL feature the project uses, before setup.

    python -m oee.cli preflight

Each check runs one tiny query. A FAIL names the SQL file that needs it.
"""
from __future__ import annotations

CHECKS = [
    # (feature, sql, expected value or None, used in)
    ("Python UDFs (script language container)", None, None, "every UDF"),
    ("CONNECT BY LEVEL row generator", "SELECT COUNT(*) FROM (SELECT LEVEL - 1 AS N FROM DUAL CONNECT BY LEVEL <= 10)", 10, "02_fusion"),
    ("ADD_MINUTES / ADD_HOURS", "SELECT HOURS_BETWEEN(ADD_HOURS(ADD_MINUTES(TIMESTAMP '2026-01-01 00:00:00', 60), 1), TIMESTAMP '2026-01-01 00:00:00')", 2, "02, 04, 10"),
    ("MINUTES_BETWEEN / SECONDS_BETWEEN", "SELECT MINUTES_BETWEEN(TIMESTAMP '2026-01-01 01:00:00', TIMESTAMP '2026-01-01 00:00:00') + SECONDS_BETWEEN(TIMESTAMP '2026-01-01 00:00:10', TIMESTAMP '2026-01-01 00:00:00')", 70, "02, 03, 06"),
    ("DATE_TRUNC('HOUR', ...)", "SELECT MINUTE(DATE_TRUNC('HOUR', TIMESTAMP '2026-01-01 05:47:00'))", 0, "02_fusion"),
    ("Gap fill: COUNT/MAX OVER running window", "SELECT MAX(G) FROM (SELECT COUNT(X) OVER (ORDER BY N ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS G FROM (SELECT LEVEL AS N, CASE WHEN LEVEL IN (1, 3) THEN 1 END AS X FROM DUAL CONNECT BY LEVEL <= 4))", 2, "02_fusion"),
    ("EDIT_DISTANCE", "SELECT EDIT_DISTANCE('CNMG120408', 'CNMG120480')", 2, "11_suppliers"),
    ("JSON_VALUE with a nested path", """SELECT CAST(JSON_VALUE('{"price": {"usd": 4.5}}', '$.price.usd') AS DOUBLE) * 2""", 9, "11_suppliers"),
    ("REGEXP_SUBSTR with occurrence", "SELECT REGEXP_SUBSTR('BRG 6205 2RS', '[A-Z]*[0-9][A-Z0-9]*', 1, 2)", "2RS", "11_suppliers"),
    ("REGEXP_REPLACE (all occurrences)", "SELECT REGEXP_REPLACE('A-B-C', '[^A-Z]', '')", "ABC", "11_suppliers"),
    ("INSTR", "SELECT INSTR('OILSEAL45X62X8', '45X62X8')", 8, "11_suppliers"),
    ("LEAST / GREATEST / CEIL / EXP / LN", "SELECT LEAST(3, 1) + GREATEST(1, 2) + CEIL(0.2) + ROUND(EXP(LN(2)), 6)", 6, "06, 09, 11"),
    ("ROW_NUMBER / LAG windows", "SELECT MAX(L) FROM (SELECT LAG(LEVEL) OVER (ORDER BY LEVEL) AS L FROM DUAL CONNECT BY LEVEL <= 3)", 2, "06, 10"),
]

UDF_PROBE = """CREATE OR REPLACE PYTHON3 SCALAR SCRIPT {schema}.PREFLIGHT_PY() RETURNS VARCHAR(200) AS
import sys
def run(ctx):
    import numpy
    return sys.version.split()[0] + " / numpy " + numpy.__version__
/
"""


def _same(got, want) -> bool:
    if want is None:
        return True
    try:
        return abs(float(got) - float(want)) < 1e-6
    except (TypeError, ValueError):
        return str(got) == str(want)


def sql_identifiers() -> dict[str, set[str]]:
    """Every name the project's SQL defines (columns, aliases, CTEs, table aliases), by file."""
    import re
    from pathlib import Path

    pats = [
        r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        r"(?:\bWITH|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(",
        r"^\s+([A-Z_][A-Z0-9_]*)\s+(?:VARCHAR|DECIMAL|DOUBLE|TIMESTAMP|DATE|BOOLEAN)",
        r"\b(?:FROM|JOIN)\s+[A-Za-z_][\w.]*\s+([a-z][a-z0-9_]*)\b",
    ]
    from oee import patterns

    texts = [(p.name, p.read_text()) for p in sorted((Path(__file__).resolve().parents[1] / "sql").glob("*.sql"))]
    texts += [("oee/patterns.py", patterns.search_sql("BEARING", True) + patterns.summary_sql("BEARING", True)
               + "".join(patterns.replay_sql("BEARING"))
               + patterns.local_parts_sql("POWER", "CHN") + patterns.network_parts_sql("POWER", "CHN"))]
    out: dict[str, set[str]] = {}
    for name, raw in texts:
        text = re.sub(r"--[^\n]*", "", raw)
        names = set()
        for p in pats:
            names |= {m.upper() for m in re.findall(p, text, flags=re.MULTILINE)}
        out[name] = names - {"SELECT", "ON", "WHERE", "JOIN", "LEFT", "CROSS", "GROUP", "ORDER", "UNION",
                                  "WITH", "DATE", "DECIMAL", "DOUBLE", "VARCHAR", "TIMESTAMP", "BOOLEAN", "BIGINT", "INTEGER"}
    return out


def run_preflight(conn, log=print) -> bool:
    ok = True
    try:
        reserved = {r[0].upper() for r in conn.execute(
            "SELECT KEYWORD FROM EXA_SQL_KEYWORDS WHERE RESERVED").fetchall()}
        clashes = {f: sorted(n & reserved) for f, n in sql_identifiers().items() if n & reserved}
        passed = not clashes
        ok &= passed
        detail = "no clashes" if passed else "; ".join(f"{f}: {', '.join(v)}" for f, v in clashes.items())
        log(f"  {'PASS' if passed else 'FAIL'}  {'No reserved words used as names':42s} {detail}")
    except Exception as exc:  # noqa: BLE001
        log(f"  SKIP  reserved-word check: {str(exc).splitlines()[0][:120]}")
    conn.execute("CREATE SCHEMA IF NOT EXISTS OEE")
    for feature, sql, want, used in CHECKS:
        try:
            if sql is None:
                conn.execute(UDF_PROBE.format(schema="OEE"))
                got = conn.execute("SELECT OEE.PREFLIGHT_PY()").fetchval()
                conn.execute("DROP SCRIPT IF EXISTS OEE.PREFLIGHT_PY")
            else:
                got = conn.execute(sql).fetchval()
            passed = _same(got, want)
            detail = f"got {got!r}" + ("" if passed else f", expected {want!r}")
        except Exception as exc:  # noqa: BLE001
            passed, detail = False, str(exc).splitlines()[0][:160]
        ok &= passed
        log(f"  {'PASS' if passed else 'FAIL'}  {feature:42s} {detail}   [{used}]")
    log("\n  all checks passed" if ok else "\n  some checks failed: send this output over before running setup")
    return ok
