# Vectorised gradient-boosted tree scorer (numpy only).
#
# This module's source is embedded into the Exasol PYTHON3 SET UDF built by
# oee/compile_udf.py:render_batch_udf, which scores whole batches of sensor
# readings at once. numpy ships with the standard python3 script language
# container.
#
# All trees are packed into one set of node arrays and walked together, level
# by level, so each step is a handful of large numpy operations instead of a
# Python loop per tree. Inputs and thresholds are float32, matching XGBoost's
# split arithmetic; tests/test_batch_udf.py checks it against xgboost.predict.
import numpy as np


def vload(spec):
    """Pack the compact model dict written by compile_udf into flat numpy arrays."""
    feats, thr, yes, no, miss, val, roots = [], [], [], [], [], [], []
    depth, offset = 0, 0
    for t in spec["trees"]:
        k = len(t["f"])
        roots.append(offset)
        feats.extend(t["f"])
        thr.extend(t["t"])
        val.extend(t["v"])
        for arr, out in ((t["y"], yes), (t["n"], no), (t["m"], miss)):
            out.extend(c + offset if f >= 0 else i + offset for i, (c, f) in enumerate(zip(arr, t["f"])))
        depth = max(depth, _depth(t))
        offset += k
    f = np.asarray(feats, dtype=np.int64)
    return {
        "f": np.where(f >= 0, f, 0),
        "leaf": f < 0,
        "t": np.asarray(thr, dtype=np.float32),
        # leaves point at themselves, so extra levels leave them in place
        "y": np.asarray(yes, dtype=np.int64),
        "n": np.asarray(no, dtype=np.int64),
        "m": np.asarray(miss, dtype=np.int64),
        "v": np.asarray(val, dtype=np.float64),
        "roots": np.asarray(roots, dtype=np.int64),
        "depth": depth,
        "base_margin": float(spec["base_margin"]),
    }


def _depth(t):
    best, stack = 0, [(0, 0)]
    while stack:
        node, d = stack.pop()
        if t["f"][node] < 0:
            best = max(best, d)
        else:
            stack.append((t["y"][node], d + 1))
            stack.append((t["n"][node], d + 1))
    return best


def vmargin(model, X, chunk=2000):
    """X: (rows, features). Returns the raw margin for every row."""
    X = np.asarray(X, dtype=np.float32)
    out = np.empty(X.shape[0], dtype=np.float64)
    for s in range(0, X.shape[0], chunk):
        out[s:s + chunk] = _margin_chunk(model, X[s:s + chunk])
    return out


def _margin_chunk(model, X):
    n = X.shape[0]
    xt = np.ascontiguousarray(X.T).ravel()          # feature-major, so one gather per level
    cols = np.arange(n, dtype=np.int64)[None, :]
    node = np.repeat(model["roots"][:, None], n, axis=1)   # (trees, rows)
    for _ in range(model["depth"]):
        x = xt[model["f"][node] * n + cols]
        go = np.where(x < model["t"][node], model["y"][node], model["n"][node])
        node = np.where(np.isnan(x), model["m"][node], go)
    return model["base_margin"] + model["v"][node].sum(axis=0)


def vproba(model, X):
    z = vmargin(model, X)
    return 1.0 / (1.0 + np.exp(-z))
