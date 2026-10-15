"""DuckDB stand-in for Exasol, used only by the test suite and for UI development.

It runs the project's own sql/*.sql files after a few mechanical dialect
translations, and registers FAILURE_RISK as a DuckDB Python UDF using the same
pure-Python scorer that is embedded in the Exasol UDF. The production
application always talks to Exasol.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

import duckdb
import xgboost as xgb

from oee import scorer
from oee.compile_udf import booster_to_spec
from oee.features import FEATURES
from oee.generate import generate
from oee.sqlrun import split_statements
from oee.train import train
from oee.features import add_labels, build_features

ROOT = Path(__file__).resolve().parents[1]

_SECONDS_BETWEEN = re.compile(r"SECONDS_BETWEEN\(\s*([\w.]+)\s*,\s*([\w.]+)\s*\)", re.IGNORECASE)
_ADD_HOURS = re.compile(r"ADD_HOURS\(\s*([\w.]+)\s*,\s*(\d+)\s*\)", re.IGNORECASE)


def translate(sql: str) -> str:
    sql = re.sub(r"OPEN\s+SCHEMA\s+OEE", "SET schema = 'oee'", sql, flags=re.IGNORECASE)
    sql = _SECONDS_BETWEEN.sub(r"(epoch(\1) - epoch(\2))", sql)
    sql = _ADD_HOURS.sub(r"(\1 + INTERVAL (\2) HOUR)", sql)
    sql = re.sub(r"\bOEE\.FAILURE_RISK\(", "FAILURE_RISK(", sql)
    return sql


class DuckBackend:
    name = "DuckDB test stand-in"

    def __init__(self, days: int = 30, seed: int = 42, per_plant: int = 8):
        self.con = duckdb.connect()
        self._lock = threading.Lock()
        tables = generate(days=days, seed=seed, per_plant=per_plant)

        feats = add_labels(build_features(tables["TELEMETRY"], tables["MACHINES"]), tables["DOWNTIME"])
        model, metrics = train(feats, n_estimators=80, max_depth=4)
        spec = booster_to_spec(model.get_booster())
        m = scorer.load_model(spec)

        n = len(FEATURES)
        params = ", ".join(f"a{i}" for i in range(n))
        ns = {"_score": lambda xs: scorer.predict_proba(m, xs)}
        exec(f"def failure_risk({params}):\n    return _score([{params}])", ns)
        failure_risk = ns["failure_risk"]

        self.con.create_function("FAILURE_RISK", failure_risk, ["DOUBLE"] * len(FEATURES), "DOUBLE",
                                 null_handling="special")

        self._run_file("01_schema.sql")
        for name, df in tables.items():
            df = df.copy()
            df.columns = [c.upper() for c in df.columns]
            self.con.register("_df", df)
            self.con.execute(f"INSERT INTO oee.{name} SELECT * FROM _df")
            self.con.unregister("_df")
        self._run_file("02_oee_views.sql")
        self._run_file("03_features.sql")
        self.con.execute(
            "INSERT INTO oee.MODEL_REGISTRY VALUES ('FAILURE_RISK', now(), ?, ?, ?, ?)",
            [metrics["n_trees"], metrics["test_auc"], metrics["test_precision_at_top5pct"], ",".join(FEATURES)],
        )
        text = (ROOT / "sql" / "04_scoring.sql").read_text().replace("{FEATURE_COLUMNS}", ", ".join(FEATURES))
        for s in split_statements(text):
            self.con.execute(translate(s))

    def _run_file(self, name: str):
        for s in split_statements((ROOT / "sql" / name).read_text()):
            self.con.execute(translate(s))

    def query(self, sql: str) -> list[dict]:
        from oee.app import _clean

        with self._lock:
            cur = self.con.execute(translate(sql))
            cols = [d[0].upper() for d in cur.description]
            return [dict(zip(cols, map(_clean, row))) for row in cur.fetchall()]

    def execute(self, sql: str) -> int:
        with self._lock:
            self.con.execute(translate(sql))
            return 1
