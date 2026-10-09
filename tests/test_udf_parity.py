"""The compiled UDF must score exactly like xgboost.predict_proba."""
import numpy as np
import pytest
import xgboost as xgb

from oee import scorer
from oee.compile_udf import booster_to_spec, render_udf
from oee.features import FEATURES, LABEL
from oee.train import train


@pytest.fixture(scope="module")
def trained(training_df):
    model, metrics = train(training_df, n_estimators=60, max_depth=4)
    return model, metrics, training_df


def test_model_beats_chance(trained):
    _, metrics, _ = trained
    assert metrics["test_auc"] > 0.7
    assert metrics["test_precision_at_top5pct"] > 2 * metrics["test_positive_rate"]


def test_pure_python_scorer_matches_xgboost(trained):
    model, _, df = trained
    X = df[FEATURES].astype(float).to_numpy()[:3000]
    expected = model.predict_proba(X)[:, 1]
    spec = booster_to_spec(model.get_booster(), calibration_X=X[:64])
    m = scorer.load_model(spec)
    got = np.array([scorer.predict_proba(m, row) for row in X])
    assert np.max(np.abs(got - expected)) < 1e-5


def test_missing_values_follow_xgboost_default_direction(trained):
    model, _, df = trained
    X = df[FEATURES].astype(float).to_numpy()[:200].copy()
    X[:, FEATURES.index("VIB_DELTA_6H")] = np.nan
    expected = model.get_booster().predict(xgb.DMatrix(X, feature_names=FEATURES))
    spec = booster_to_spec(model.get_booster())
    m = scorer.load_model(spec)
    got = np.array([scorer.predict_proba(m, list(row)) for row in X])
    assert np.max(np.abs(got - expected)) < 1e-5


class _Ctx:
    """Minimal stand-in for an Exasol UDF context (supports ctx[i] and ctx.NAME)."""

    def __init__(self, values):
        self._values = list(values)
        for name, v in zip(FEATURES, values):
            setattr(self, name, v)

    def __getitem__(self, i):
        return self._values[i]


def _exec_udf_body(sql: str) -> dict:
    body = sql.split(" AS\n", 1)[1].rsplit("\n/\n", 1)[0]
    ns: dict = {}
    exec(compile(body, "<udf>", "exec"), ns)
    return ns


def test_rendered_udf_runs_and_matches(trained):
    model, _, df = trained
    sql = render_udf(booster_to_spec(model.get_booster()))
    assert sql.startswith("CREATE OR REPLACE PYTHON3 SCALAR SCRIPT OEE.FAILURE_RISK(")
    assert sql.rstrip().endswith("/")
    ns = _exec_udf_body(sql)
    X = df[FEATURES].astype(float).to_numpy()[:500]
    expected = model.predict_proba(X)[:, 1]
    got = np.array([ns["run"](_Ctx(row)) for row in X])
    assert np.max(np.abs(got - expected)) < 1e-5


def test_udf_body_imports_only_stdlib(trained):
    model, _, _ = trained
    sql = render_udf(booster_to_spec(model.get_booster()))
    imports = {line.split()[1].split(".")[0] for line in sql.splitlines()
               if line.startswith(("import ", "from "))}
    assert imports <= {"math", "struct", "json"}


def test_udf_parameters_follow_feature_order(trained):
    model, _, _ = trained
    sql = render_udf(booster_to_spec(model.get_booster()))
    header = sql.split(") RETURNS", 1)[0]
    params = [line.strip().split()[0] for line in header.splitlines()[1:]]
    assert params == FEATURES
    assert LABEL not in params
