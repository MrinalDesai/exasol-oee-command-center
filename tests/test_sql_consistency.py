"""Static checks that the SQL layer agrees with the Python feature contract."""
import re
from pathlib import Path

from oee.features import FEATURES, LABEL
from oee.sqlrun import split_statements

SQL = Path(__file__).resolve().parents[1] / "sql"


def test_feature_view_defines_every_model_feature():
    text = (SQL / "04_features.sql").read_text().upper()
    for f in FEATURES:
        assert re.search(rf"\b{f}\b", text), f"{f} missing from V_FEATURES"
    assert LABEL in text


def test_scoring_sql_uses_feature_placeholder():
    text = (SQL / "05_scoring.sql").read_text()
    assert "FAILURE_RISK({FEATURE_COLUMNS})" in text


def test_statement_splitter_handles_scripts_and_comments():
    sql = """-- comment; with semicolon
CREATE SCHEMA X;
CREATE OR REPLACE PYTHON3 SCALAR SCRIPT X.F(a DOUBLE) RETURNS DOUBLE AS
def run(ctx):
    return 1.0;  # semicolon inside script body
/
SELECT 'a;b' FROM DUAL;
"""
    stmts = split_statements(sql)
    assert len(stmts) == 3
    assert stmts[1].startswith("CREATE OR REPLACE PYTHON3 SCALAR SCRIPT")
    assert stmts[1].rstrip().endswith("semicolon inside script body")
    assert stmts[2] == "SELECT 'a;b' FROM DUAL"


def test_all_sql_files_split_cleanly():
    for path in sorted(SQL.glob("*.sql")):
        stmts = split_statements(path.read_text())
        assert stmts, path
        assert all(s.strip() for s in stmts)
