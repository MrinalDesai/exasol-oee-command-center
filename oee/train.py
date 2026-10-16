"""Train the 24-hour failure-risk model.

Training data comes from Exasol (OEE.V_TRAINING, features computed in-database)
by default, or from the generated CSVs with --source csv for offline work.

Usage:
    python -m oee.train                     # from Exasol
    python -m oee.train --source csv --data data/generated
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from oee.features import FEATURES, LABEL, add_labels, build_features

MODEL_DIR = Path("models")


def load_training_csv(data_dir: Path) -> pd.DataFrame:
    tel = pd.read_csv(data_dir / "telemetry.csv")
    mach = pd.read_csv(data_dir / "machines.csv")
    down = pd.read_csv(data_dir / "downtime.csv")
    return add_labels(build_features(tel, mach), down)


def load_training_exasol() -> pd.DataFrame:
    from oee.db import connect

    with connect() as conn:
        df = conn.export_to_pandas("SELECT * FROM OEE.V_TRAINING")
    df["READING_TS"] = pd.to_datetime(df["READING_TS"])
    return df


def time_split(df: pd.DataFrame, test_frac: float = 0.2, ts_col: str = "READING_TS"):
    ts = pd.to_datetime(df[ts_col])
    cutoff = ts.quantile(1 - test_frac)
    return df[ts < cutoff], df[ts >= cutoff], cutoff


def precision_at_k(y_true, scores, frac=0.05):
    k = max(1, int(len(scores) * frac))
    top = np.argsort(-scores)[:k]
    return float(np.asarray(y_true)[top].mean())


def train(df: pd.DataFrame, n_estimators=200, max_depth=4, seed=42,
          features: list[str] = FEATURES, label: str = LABEL, ts_col: str = "READING_TS"):
    train_df, test_df, cutoff = time_split(df, ts_col=ts_col)
    X_tr = train_df[features].astype(float)
    y_tr = train_df[label].astype(int)
    X_te = test_df[features].astype(float)
    y_te = test_df[label].astype(int)

    # No class re-weighting: it would inflate every probability (a "47%" that
    # fails 7% of the time). Unweighted training keeps the risk scores
    # calibrated, which the console relies on, and ranks as well or better.
    model = xgb.XGBClassifier(
        n_estimators=n_estimators,
        max_depth=max_depth,
        learning_rate=0.08,
        subsample=0.9,
        colsample_bytree=0.9,
        min_child_weight=5,
        eval_metric="aucpr",
        random_state=seed,
        n_jobs=4,
    )
    model.fit(X_tr, y_tr)
    p = model.predict_proba(X_te)[:, 1]
    metrics = {
        "train_rows": int(len(train_df)),
        "test_rows": int(len(test_df)),
        "split_cutoff": str(cutoff),
        "test_positive_rate": float(y_te.mean()),
        "test_auc": float(roc_auc_score(y_te, p)),
        "test_average_precision": float(average_precision_score(y_te, p)),
        "test_precision_at_top5pct": precision_at_k(y_te, p, 0.05),
        "test_brier": float(brier_score_loss(y_te, p)),
        "test_mean_predicted": float(p.mean()),
        "n_trees": n_estimators,
        "max_depth": max_depth,
        "features": list(features),
    }
    return model, metrics


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["exasol", "csv"], default="exasol")
    ap.add_argument("--data", default="data/generated")
    ap.add_argument("--out", default=str(MODEL_DIR))
    args = ap.parse_args()

    df = load_training_exasol() if args.source == "exasol" else load_training_csv(Path(args.data))
    model, metrics = train(df)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    model.get_booster().save_model(out / "failure_risk.json")
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps({k: v for k, v in metrics.items() if k != "features"}, indent=2))


if __name__ == "__main__":
    main()
