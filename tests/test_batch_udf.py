"""The vectorised batch UDF must score exactly like xgboost, and its SQL must be well formed."""
import numpy as np
import pandas as pd
import pytest
import xgboost as xgb

from oee import vscorer
from oee.compile_udf import booster_to_spec, render_batch_udf
from oee.features import FEATURES
from oee.train import train


@pytest.fixture(scope="module")
def model_and_X(training_df):
    model, _ = train(training_df, n_estimators=80, max_depth=4)
    X = training_df[FEATURES].astype(float).to_numpy()
    return model, X, training_df


def test_vectorised_scorer_matches_xgboost(model_and_X):
    model, X, _ = model_and_X
    spec = booster_to_spec(model.get_booster(), calibration_X=X[:64])
    got = vscorer.vproba(vscorer.vload(spec), X)
    assert np.max(np.abs(got - model.predict_proba(X)[:, 1])) < 1e-5


def test_vectorised_scorer_handles_missing_values(model_and_X):
    model, X, _ = model_and_X
    Xm = X[:500].copy()
    Xm[::3, FEATURES.index("VIB_DELTA_6H")] = np.nan
    Xm[::5, FEATURES.index("TOOL_WEAR_MIN")] = np.nan
    spec = booster_to_spec(model.get_booster())
    got = vscorer.vproba(vscorer.vload(spec), Xm)
    expected = model.get_booster().predict(xgb.DMatrix(Xm, feature_names=FEATURES))
    assert np.max(np.abs(got - expected)) < 1e-5


class _SetCtx:
    """Minimal stand-in for an Exasol SET-script context: get_dataframe() batches, emit()."""

    def __init__(self, df):
        self.df, self.pos, self.out = df, 0, []

    def get_dataframe(self, num_rows="all", start_col=0):
        if self.pos >= len(self.df):
            return None
        n = len(self.df) if num_rows == "all" else num_rows
        chunk = self.df.iloc[self.pos:self.pos + n, start_col:].reset_index(drop=True)
        self.pos += n
        return chunk

    def emit(self, frame):
        self.out.append(frame)


def test_rendered_batch_udf_runs_and_matches(model_and_X):
    model, X, df = model_and_X
    sql = render_batch_udf(booster_to_spec(model.get_booster()), batch_rows=777)
    assert sql.startswith("CREATE OR REPLACE PYTHON3 SET SCRIPT OEE.FAILURE_RISK_BATCH(")
    assert "EMITS (MACHINE_ID VARCHAR(32), READING_TS TIMESTAMP, RISK_24H DOUBLE)" in sql
    body = sql.split(" AS\n", 1)[1].rsplit("\n/\n", 1)[0]
    ns = {}
    exec(compile(body, "<batch-udf>", "exec"), ns)
    inp = pd.concat([df[["MACHINE_ID", "READING_TS"]].reset_index(drop=True),
                     df[FEATURES].astype(float).reset_index(drop=True)], axis=1).iloc[:3000]
    ctx = _SetCtx(inp)
    ns["run"](ctx)
    out = pd.concat(ctx.out, ignore_index=True)
    assert len(out) == 3000 and list(out.columns) == ["MACHINE_ID", "READING_TS", "RISK_24H"]
    assert (out["MACHINE_ID"].values == inp["MACHINE_ID"].values).all()
    expected = model.predict_proba(inp[FEATURES].to_numpy())[:, 1]
    assert np.max(np.abs(out["RISK_24H"].to_numpy() - expected)) < 1e-5


def test_batch_udf_imports_only_container_packages(model_and_X):
    model, _, _ = model_and_X
    sql = render_batch_udf(booster_to_spec(model.get_booster()))
    imports = {line.split()[1].split(".")[0] for line in sql.splitlines() if line.startswith(("import ", "from "))}
    assert imports <= {"numpy", "json", "pandas", "time"}
