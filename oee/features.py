"""Feature definitions shared by training, the UDF compiler and the SQL layer.

FEATURES is the single source of truth for the model's input order. The SQL
view OEE.V_FEATURES (sql/04_features.sql) computes the same columns in-database;
`build_features` is the pandas reference implementation used for offline
training and tests.
"""
from __future__ import annotations

import pandas as pd

FEATURES = [
    "AIR_TEMP_K",
    "PROCESS_TEMP_K",
    "TEMP_DIFF_K",
    "RPM",
    "TORQUE_NM",
    "TORQUE_RATIO",
    "TOOL_WEAR_MIN",
    "WEAR_X_LOAD",
    "VIBRATION_MM_S",
    "POWER_KW",
    "VIB_AVG_6H",
    "VIB_DELTA_6H",
    "TEMP_DIFF_AVG_6H",
    "POWER_AVG_6H",
    "IS_MILL",
    "IS_LATHE",
    "IS_PRESS",
    "IS_PACKER",
]
LABEL = "LABEL_FAIL_24H"


def build_features(telemetry: pd.DataFrame, machines: pd.DataFrame) -> pd.DataFrame:
    """Pandas mirror of OEE.V_FEATURES. Column names are upper-case like Exasol."""
    t = telemetry.copy()
    t.columns = [c.upper() for c in t.columns]
    m = machines.copy()
    m.columns = [c.upper() for c in m.columns]
    t["READING_TS"] = pd.to_datetime(t["READING_TS"])
    df = t.merge(m[["MACHINE_ID", "PLANT_ID", "MACHINE_TYPE", "NOMINAL_TORQUE"]], on="MACHINE_ID")
    df = df.sort_values(["MACHINE_ID", "READING_TS"]).reset_index(drop=True)

    df["TEMP_DIFF_K"] = df["PROCESS_TEMP_K"] - df["AIR_TEMP_K"]
    df["TORQUE_RATIO"] = df["TORQUE_NM"] / df["NOMINAL_TORQUE"]
    df["WEAR_X_LOAD"] = df["TOOL_WEAR_MIN"] * df["TORQUE_NM"] / df["NOMINAL_TORQUE"]

    g = df.groupby("MACHINE_ID", sort=False)
    df["VIB_AVG_6H"] = g["VIBRATION_MM_S"].transform(lambda s: s.rolling(6, min_periods=1).mean())
    lag6 = g["VIBRATION_MM_S"].shift(6)
    df["VIB_DELTA_6H"] = df["VIBRATION_MM_S"] - lag6.fillna(df["VIBRATION_MM_S"])
    df["TEMP_DIFF_AVG_6H"] = g["TEMP_DIFF_K"].transform(lambda s: s.rolling(6, min_periods=1).mean())
    df["POWER_AVG_6H"] = g["POWER_KW"].transform(lambda s: s.rolling(6, min_periods=1).mean())

    for col, mtype in [("IS_MILL", "CNC_MILL"), ("IS_LATHE", "CNC_LATHE"),
                       ("IS_PRESS", "PRESS"), ("IS_PACKER", "PACKER")]:
        df[col] = (df["MACHINE_TYPE"] == mtype).astype(int)
    return df.drop(columns=["NOMINAL_TORQUE"])


def add_labels(features: pd.DataFrame, downtime: pd.DataFrame, horizon_hours: int = 24) -> pd.DataFrame:
    """Pandas mirror of OEE.V_TRAINING: an unplanned stop starts in (ts, ts + horizon]."""
    d = downtime.copy()
    d.columns = [c.upper() for c in d.columns]
    d = d[d["STOP_TYPE"] == "UNPLANNED"].copy()
    d["START_TS"] = pd.to_datetime(d["START_TS"])
    out = features.copy()
    out[LABEL] = 0
    horizon = pd.Timedelta(hours=horizon_hours)
    for mid, stops in d.groupby("MACHINE_ID"):
        idx = out.index[out["MACHINE_ID"] == mid]
        ts = out.loc[idx, "READING_TS"].values
        starts = stops["START_TS"].sort_values().values
        # first stop strictly after ts; positive if it is within the horizon
        pos = starts.searchsorted(ts, side="right")
        has_next = pos < len(starts)
        nxt = starts[pos.clip(max=len(starts) - 1)]
        out.loc[idx, LABEL] = (has_next & (nxt <= ts + horizon)).astype(int)
    return out
