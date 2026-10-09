# Pure-Python gradient-boosted tree scorer. No third-party imports.
#
# This module's source is embedded verbatim into the Exasol PYTHON3 UDF by
# oee/compile_udf.py, so it must run on the stock script language container.
# Inputs and thresholds are rounded to float32 to match XGBoost's own split
# arithmetic; tests/test_udf_parity.py checks it against xgboost.predict.
import math
import struct


def _f32(v):
    return struct.unpack("f", struct.pack("f", v))[0]


def load_model(spec):
    """Prepare a compact model dict (as written by compile_udf) for scoring."""
    trees = []
    for t in spec["trees"]:
        trees.append(
            (
                t["f"],                          # split feature index, -1 for leaf
                [_f32(v) for v in t["t"]],       # split threshold (float32)
                t["y"],                          # child if x < threshold
                t["n"],                          # child otherwise
                t["m"],                          # child if x is missing
                t["v"],                          # leaf value
            )
        )
    return {"features": spec["features"], "base_margin": spec["base_margin"], "trees": trees}


def predict_margin(model, x):
    xs = [None if (v is None or v != v) else _f32(float(v)) for v in x]
    total = 0.0
    for f, thr, yes, no, miss, val in model["trees"]:
        node = 0
        while f[node] >= 0:
            v = xs[f[node]]
            if v is None:
                node = miss[node]
            elif v < thr[node]:
                node = yes[node]
            else:
                node = no[node]
        total += val[node]
    return model["base_margin"] + total


def predict_proba(model, x):
    z = predict_margin(model, x)
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)
