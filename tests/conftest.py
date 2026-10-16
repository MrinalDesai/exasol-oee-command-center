import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oee.features import add_labels, build_features  # noqa: E402
from oee.generate import generate  # noqa: E402


@pytest.fixture(scope="session")
def tables():
    # Small but non-trivial fleet so the suite stays fast
    return generate(days=30, seed=7, per_plant=6)


@pytest.fixture(scope="session")
def training_df(tables):
    feats = build_features(tables["TELEMETRY"], tables["MACHINES"])
    return add_labels(feats, tables["DOWNTIME"])


@pytest.fixture(scope="session")
def duck():
    """The project's SQL running on DuckDB (see tests/fakedb.py)."""
    from tests.fakedb import DuckBackend

    return DuckBackend(days=30, seed=42)
