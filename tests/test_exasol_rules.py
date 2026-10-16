"""Exasol rules DuckDB does not enforce: every computed column in a view, CTAS,
CTE or FROM-subquery must be named ("must name expression in query ...")."""
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
from sqlglot import exp  # noqa: E402

from oee.pipeline import deploy_steps  # noqa: E402
from oee.sqlrun import split_statements  # noqa: E402
from tests.fakedb import translate  # noqa: E402

SQL = Path(__file__).resolve().parents[1] / "sql"


def _needs_names(sel) -> bool:
    node, child = sel.parent, sel
    while node is not None:
        if isinstance(node, (exp.CTE, exp.Create)):
            return True
        if isinstance(node, exp.Subquery):
            return isinstance(node.parent, (exp.From, exp.Join)) or node.parent is None
        if isinstance(node, exp.Union):
            if child is not node.this:
                return False          # later UNION branches take the first branch's names
            child, node = node, node.parent
            continue
        return False
    return False


def _statements():
    from oee import patterns

    texts = [(p.name, p.read_text()) for p in sorted(SQL.glob("0[1-4]*.sql"))] + deploy_steps()
    for mode in patterns.MODES:
        texts += [("patterns.search", patterns.search_sql(mode, False)), ("patterns.summary", patterns.summary_sql(mode, False))]
    texts += [("patterns.replay", patterns.replay_sql("BEARING")[0])]
    for plant in ("PNQ", "BLR", "CHN"):
        texts += [("patterns.local", patterns.local_parts_sql("POWER", plant)),
                  ("patterns.network", patterns.network_parts_sql("POWER", plant))]
    for name, text in texts:
        for st in split_statements(text):
            if st.strip().upper().startswith(("OPEN", "INSERT", "CREATE OR REPLACE PYTHON")):
                continue
            yield name, st


def test_every_computed_column_is_named():
    unnamed = []
    for name, st in _statements():
        tree = sqlglot.parse_one(translate(st).replace("/**/", ""), read="duckdb")
        for sel in tree.find_all(exp.Select):
            if _needs_names(sel):
                unnamed += [f"{name}: {e.sql()[:80]}" for e in sel.expressions
                            if not isinstance(e, (exp.Alias, exp.Column, exp.Star))]
    assert not unnamed, "\n".join(unnamed)


def test_no_name_is_an_exasol_reserved_word():
    """tests/exasol_reserved.txt is EXA_SQL_KEYWORDS WHERE RESERVED, from Exasol Personal."""
    from oee.preflight import sql_identifiers

    reserved = set((Path(__file__).parent / "exasol_reserved.txt").read_text().split())
    clashes = {f: sorted(n & reserved) for f, n in sql_identifiers().items() if n & reserved}
    assert not clashes, clashes
