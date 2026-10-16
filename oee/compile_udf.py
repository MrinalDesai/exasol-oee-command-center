"""Compile a trained XGBoost booster into a dependency-free Exasol PYTHON3 UDF.

The trees are flattened into compact arrays, embedded as JSON in the script
body together with the source of oee/scorer.py. The resulting UDF runs on the
stock python3 script language container: no BucketFS upload, no custom SLC.

Usage:
    python -m oee.compile_udf --model models/failure_risk.json --out build/udf_failure_risk.sql
"""
from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path

import numpy as np
import xgboost as xgb

from oee import scorer, vscorer
from oee.features import FEATURES

UDF_NAME = "OEE.FAILURE_RISK"


def _flatten_tree(tree: dict, feat_index: dict[str, int]) -> dict:
    nodes = {}

    def walk(n):
        nodes[n["nodeid"]] = n
        for c in n.get("children", []):
            walk(c)

    walk(tree)
    size = max(nodes) + 1
    f, t, y, no, m, v = [-1] * size, [0.0] * size, [0] * size, [0] * size, [0] * size, [0.0] * size
    for i, n in nodes.items():
        if "leaf" in n:
            v[i] = float(n["leaf"])
        else:
            split = n["split"]
            f[i] = feat_index[split] if split in feat_index else int(split.lstrip("f"))
            t[i] = float(n["split_condition"])
            y[i], no[i], m[i] = n["yes"], n["no"], n["missing"]
    return {"f": f, "t": t, "y": y, "n": no, "m": m, "v": v}


def booster_to_spec(booster: xgb.Booster, calibration_X: np.ndarray | None = None,
                    features: list[str] | None = None) -> dict:
    FEATURES = features or globals()["FEATURES"]   # noqa: N806  (default: the failure model)
    feat_index = {name: i for i, name in enumerate(FEATURES)}
    trees = [_flatten_tree(json.loads(d), feat_index) for d in booster.get_dump(dump_format="json")]
    spec = {"features": FEATURES, "base_margin": 0.0, "trees": trees}

    # Recover the base margin empirically so this works across XGBoost versions
    # (base_score storage/format has changed between releases).
    if calibration_X is None:
        rng = np.random.default_rng(0)
        calibration_X = rng.normal(size=(64, len(FEATURES)))
    dm = xgb.DMatrix(calibration_X, feature_names=FEATURES)
    xgb_margin = booster.predict(dm, output_margin=True)
    model = scorer.load_model(spec)
    tree_sum = np.array([scorer.predict_margin(model, row) for row in calibration_X])
    diffs = xgb_margin - tree_sum
    if np.ptp(diffs) > 1e-4:
        raise RuntimeError(f"Tree export mismatch: margin offsets vary by {np.ptp(diffs):.2e}")
    spec["base_margin"] = float(np.median(diffs))
    return spec


def render_udf(spec: dict, name: str = UDF_NAME) -> str:
    params = ",\n    ".join(f"{f} DOUBLE" for f in spec["features"])
    model_json = json.dumps(spec, separators=(",", ":"))
    names = ", ".join(repr(f) for f in spec["features"])
    body = inspect.getsource(scorer)
    return f"""CREATE OR REPLACE PYTHON3 SCALAR SCRIPT {name}(
    {params}
) RETURNS DOUBLE AS
{body}
import json as _json

_MODEL = load_model(_json.loads(r'''{model_json}'''))
_NAMES = [{names}]
_N = len(_NAMES)


def _arg(ctx, i):
    try:
        return ctx[i]
    except Exception:
        name = _NAMES[i]
        return getattr(ctx, name, getattr(ctx, name.lower(), None))


def run(ctx):
    return predict_proba(_MODEL, [_arg(ctx, i) for i in range(_N)])
/
"""


BATCH_UDF_NAME = "FAILURE_RISK_BATCH"


def render_batch_udf(spec: dict, name: str = "OEE." + BATCH_UDF_NAME, batch_rows: int = 20000,
                     timing: bool = False) -> str:
    """A SET script that scores readings in numpy batches. Each reading is scored
    independently, so the caller can group the rows however it likes; Exasol runs
    the groups in parallel across CPU cores, and each instance loads the model once
    and reuses it for every group it is given."""
    params = ",\n    ".join(f"{f} DOUBLE" for f in spec["features"])
    model_json = json.dumps(spec, separators=(",", ":"))
    body = inspect.getsource(vscorer)
    if timing:
        # Diagnostic variant: same work, but emits where each instance spent its time
        emits = "EMITS (MACHINE_ID VARCHAR(32), N_ROWS DECIMAL(12,0), T_MODULE DOUBLE, T_LOAD DOUBLE, T_START DOUBLE, T_FETCH DOUBLE, T_SCORE DOUBLE, T_EMIT DOUBLE, T_END DOUBLE, VM_PID DECIMAL(12,0))"
        run = """
def run(ctx):
    import time
    t_start = time.time()
    fetch = score = emit = 0.0
    n = 0
    mid = None
    while True:
        a = time.time()
        df = ctx.get_dataframe(num_rows=_BATCH)
        fetch += time.time() - a
        if df is None or len(df) == 0:
            break
        mid = df.iloc[0, 0]
        a = time.time()
        risk = vproba(_MODEL, df.iloc[:, 2:].to_numpy(dtype="float32", na_value=float("nan")))
        score += time.time() - a
        a = time.time()
        out = _pd.DataFrame({"MACHINE_ID": df.iloc[:, 0], "READING_TS": df.iloc[:, 1], "RISK_24H": risk})
        emit += time.time() - a
        n += len(df)
    import os
    ctx.emit(mid, n, _T0, _T_LOAD, t_start, fetch, score, emit, time.time(), os.getpid())
"""
    else:
        emits = "EMITS (MACHINE_ID VARCHAR(32), READING_TS TIMESTAMP, RISK_24H DOUBLE)"
        run = """
def run(ctx):
    while True:
        df = ctx.get_dataframe(num_rows=_BATCH)
        if df is None or len(df) == 0:
            break
        risk = vproba(_MODEL, df.iloc[:, 2:].to_numpy(dtype="float32", na_value=float("nan")))
        ctx.emit(_pd.DataFrame({"MACHINE_ID": df.iloc[:, 0], "READING_TS": df.iloc[:, 1], "RISK_24H": risk}))
"""
    return f"""CREATE OR REPLACE PYTHON3 SET SCRIPT {name}(
    MACHINE_ID VARCHAR(32),
    READING_TS TIMESTAMP,
    {params}
) {emits} AS
import time as _time
_T0 = _time.time()
{body}
import json as _json
import pandas as _pd

_MODEL = vload(_json.loads(r'''{model_json}'''))
_BATCH = {int(batch_rows)}
_T_LOAD = _time.time() - _T0
{run}
/
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="models/failure_risk.json")
    ap.add_argument("--out", default="build/udf_failure_risk.sql")
    args = ap.parse_args()
    booster = xgb.Booster()
    booster.load_model(args.model)
    spec = booster_to_spec(booster)
    sql = render_udf(spec)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(sql)
    print(f"Wrote {out} ({len(sql) / 1024:.0f} KiB, {len(spec['trees'])} trees, base_margin={spec['base_margin']:.4f})")


if __name__ == "__main__":
    main()
