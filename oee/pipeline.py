"""The ordered list of SQL steps run after the model is deployed.

Used by `oee.cli deploy` and by the test stand-in, so both run exactly the same
statements in the same order.
"""
from __future__ import annotations

import re
from pathlib import Path

from oee.features import FEATURES
from oee.quality import QUALITY_FEATURES

SQL = Path(__file__).resolve().parents[1] / "sql"


def naive_features_sql() -> str:
    """V_FEATURES_NAIVE: the same feature definitions as V_FEATURES, over TELEMETRY_NAIVE."""
    text = (SQL / "04_features.sql").read_text()
    text = re.sub(r"\bFROM TELEMETRY t\b", "FROM TELEMETRY_NAIVE t", text)
    text = re.sub(r"\bV_FEATURES\b", "V_FEATURES_NAIVE", text)
    text = re.sub(r"\bV_TRAINING\b", "V_TRAINING_NAIVE", text)
    return text


PRE_DEPLOY = ["01_schema.sql", "02_fusion.sql", "03_oee_views.sql", "03_stores.sql", "04_features.sql",
              "04_quality_features.sql"]


def deploy_steps() -> list[tuple[str, str]]:
    """(name, sql text) for every step after the UDF exists, in order."""
    cols = ", ".join(FEATURES)
    qcols = ", ".join(QUALITY_FEATURES)
    steps = []
    for path in sorted(SQL.glob("[0-9][0-9]_*.sql")):
        if path.name < "05":
            continue
        if path.name.startswith("08_"):
            steps.append(("04_features.sql (naive copy)", naive_features_sql()))
        steps.append((path.name, path.read_text().replace("{FEATURE_COLUMNS}", cols).replace("{QUALITY_COLUMNS}", qcols)))
    return steps
