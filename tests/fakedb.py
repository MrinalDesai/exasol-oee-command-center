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
from oee.generate import generate_plant
from oee.sqlrun import split_statements
from oee.train import train

ROOT = Path(__file__).resolve().parents[1]

_SECONDS_BETWEEN = re.compile(r"SECONDS_BETWEEN\(\s*([\w.]+)\s*,\s*([\w.]+)\s*\)", re.IGNORECASE)
_ADD_HOURS = re.compile(r"ADD_HOURS\(\s*([\w.]+)\s*,\s*(\d+)\s*\)", re.IGNORECASE)


def _split_args(s: str) -> list[str]:
    depth, cur, out = 0, [], []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return out


def _rewrite_func(sql: str, name: str, fn) -> str:
    """Replace NAME(arg, ...) calls, with nested parentheses, using fn(args) -> str."""
    pat = re.compile(rf"\b{name}\s*\(", re.IGNORECASE)
    while True:
        m = pat.search(sql)
        if not m:
            return sql
        i, depth = m.end(), 1
        while depth:
            depth += {"(": 1, ")": -1}.get(sql[i], 0)
            i += 1
        args = _split_args(sql[m.end():i - 1])
        sql = sql[:m.start()] + fn(args) + sql[i:]


def translate(sql: str) -> str:
    """Exasol SQL -> DuckDB SQL, for the handful of dialect differences this project uses."""
    sql = re.sub(r"OPEN\s+SCHEMA\s+OEE", "SET schema = 'oee'", sql, flags=re.IGNORECASE)
    sql = _SECONDS_BETWEEN.sub(r"(epoch(\1) - epoch(\2))", sql)
    sql = _ADD_HOURS.sub(r"(\1 + INTERVAL (\2) HOUR)", sql)
    sql = _rewrite_func(sql, "ADD_HOURS", lambda a: f"({a[0]} + to_hours(CAST({a[1]} AS BIGINT)))")
    sql = _rewrite_func(sql, "ADD_MINUTES", lambda a: f"({a[0]} + to_minutes(CAST({a[1]} AS BIGINT)))")
    sql = _rewrite_func(sql, "ADD_DAYS", lambda a: f"({a[0]} + to_days(CAST({a[1]} AS INTEGER)))")
    sql = _rewrite_func(sql, "MINUTES_BETWEEN", lambda a: f"((epoch({a[0]}) - epoch({a[1]})) / 60.0)")
    sql = _rewrite_func(sql, "HOURS_BETWEEN", lambda a: f"((epoch({a[0]}) - epoch({a[1]})) / 3600.0)")
    sql = _rewrite_func(sql, "EDIT_DISTANCE", lambda a: f"levenshtein({a[0]}, {a[1]})")
    sql = _rewrite_func(sql, "JSON_VALUE", lambda a: f"json_extract_string({a[0]}, {a[1]})")
    sql = _rewrite_func(sql, "REGEXP_SUBSTR", lambda a: f"regexp_extract({a[0]}, {a[1]})" if len(a) == 2
                        else f"(regexp_extract_all({a[0]}, {a[1]})[{a[3]}])")
    sql = _rewrite_func(sql, "REGEXP_REPLACE", lambda a: f"regexp_replace/**/({a[0]}, {a[1]}, {a[2]}, 'g')")
    sql = re.sub(r"SELECT\s+LEVEL\s*-\s*1\s+AS\s+N\s+FROM\s+DUAL\s+CONNECT\s+BY\s+LEVEL\s*<=\s*(\d+)",
                 r"SELECT range AS N FROM range(\1)", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bOEE\.(FAILURE_RISK|QUALITY_RISK)\(", r"\1(", sql)
    return sql


class DuckBackend:
    name = "DuckDB test stand-in"

    def __init__(self, days: int = 30, seed: int = 42, per_plant: int = 8, n_estimators: int = 80):
        self.con = duckdb.connect()
        self._lock = threading.Lock()
        self.tables = generate_plant(days=days, seed=seed, per_plant=per_plant)

        # same order as `oee.cli setup` / `deploy`
        self._run_file("01_schema.sql")
        for name, df in self.tables.items():
            if name == "TELEMETRY_TRUTH":
                continue
            df = df.copy()
            df.columns = [c.upper() for c in df.columns]
            self.con.register("_df", df)
            target = name if "." in name else f"oee.{name}"
            self.con.execute(f"INSERT INTO {target} SELECT * FROM _df")
            self.con.unregister("_df")
        self._run_file("02_fusion.sql")
        self._run_file("03_oee_views.sql")
        self._run_file("03_stores.sql")
        self._run_file("04_features.sql")
        self._run_file("04_quality_features.sql")

        # train on the features Exasol would compute from the fused data
        train_df = self.con.execute("SELECT * FROM oee.V_TRAINING").df()
        model, metrics = train(train_df, n_estimators=n_estimators, max_depth=4)
        self.model, self.metrics = model, metrics
        spec = booster_to_spec(model.get_booster())
        m = scorer.load_model(spec)

        n = len(FEATURES)
        params = ", ".join(f"a{i}" for i in range(n))
        ns = {"_score": lambda xs: scorer.predict_proba(m, xs)}
        exec(f"def failure_risk({params}):\n    return _score([{params}])", ns)
        self.con.create_function("FAILURE_RISK", ns["failure_risk"], ["DOUBLE"] * n, "DOUBLE",
                                 null_handling="special")

        self.con.execute(
            "INSERT INTO oee.MODEL_REGISTRY VALUES ('FAILURE_RISK', now(), ?, ?, ?, ?, ?)",
            [metrics["n_trees"], metrics["test_auc"], metrics["test_precision_at_top5pct"], ",".join(FEATURES),
             str(metrics["split_cutoff"])[:19]],
        )
        # Step 4: next-shift scrap model, registered as QUALITY_RISK
        from oee.quality import QUALITY_FEATURES, train_quality

        qdf = self.con.execute("SELECT * FROM oee.V_QUALITY_TRAINING").df()
        qmodel, qmetrics = train_quality(qdf)
        self.qmodel, self.qmetrics = qmodel, qmetrics
        qm = scorer.load_model(booster_to_spec(qmodel.get_booster(), features=QUALITY_FEATURES))
        nq = len(QUALITY_FEATURES)
        qparams = ", ".join(f"a{i}" for i in range(nq))
        qns = {"_score": lambda xs: scorer.predict_proba(qm, xs)}
        exec(f"def quality_risk({qparams}):\n    return _score([{qparams}])", qns)
        self.con.create_function("QUALITY_RISK", qns["quality_risk"], ["DOUBLE"] * nq, "DOUBLE",
                                 null_handling="special")
        self.con.execute(
            "INSERT INTO oee.MODEL_REGISTRY VALUES ('QUALITY_RISK', now(), ?, ?, ?, ?, ?)",
            [qmetrics["n_trees"], qmetrics["test_auc"], qmetrics["test_precision_at_top5pct"],
             ",".join(QUALITY_FEATURES), str(qmetrics["split_cutoff"])[:19]],
        )
        from oee.pipeline import deploy_steps

        for _name, text in deploy_steps():
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
