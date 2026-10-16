"""Step 4: quality issues caught before the end of the line.

A second XGBoost model predicts, from the machine's condition at the start of a
shift, whether that shift will scrap more than BAD_SHIFT_SCRAP of its output.
It is compiled into the Exasol UDF OEE.QUALITY_RISK exactly like FAILURE_RISK.
Features are computed in-database (sql/04_quality_features.sql).
"""
from __future__ import annotations

QUALITY_UDF = "OEE.QUALITY_RISK"
BAD_SHIFT_SCRAP = 0.07      # must match LABEL_BAD_SHIFT in sql/04_quality_features.sql
QUALITY_FEATURES = [
    "TOOL_WEAR_MIN",
    "VIB_AVG_6H",
    "TEMP_DIFF_AVG_6H",
    "TORQUE_RATIO",
    "POWER_AVG_6H",
    "PREV_SCRAP_RATE",
    "IS_MILL",
    "IS_LATHE",
    "IS_PRESS",
    "IS_PACKER",
]
QUALITY_LABEL = "LABEL_BAD_SHIFT"


def train_quality(df, n_estimators: int = 150, max_depth: int = 3):
    from oee.train import train

    return train(df, n_estimators=n_estimators, max_depth=max_depth,
                 features=QUALITY_FEATURES, label=QUALITY_LABEL, ts_col="SHIFT_START")
