import numpy as np
import pandas as pd

from oee.features import FEATURES, LABEL
from oee.generate import generate


def test_generator_is_deterministic():
    a = generate(days=5, seed=11, per_plant=2)
    b = generate(days=5, seed=11, per_plant=2)
    for name in a:
        pd.testing.assert_frame_equal(a[name], b[name])


def test_generator_tables_and_keys(tables):
    assert set(tables) == {"MACHINES", "TELEMETRY", "DOWNTIME", "MAINT_NOTES", "SHIFT_PRODUCTION", "SPARE_PARTS"}
    machine_ids = set(tables["MACHINES"].machine_id)
    for name in ("TELEMETRY", "DOWNTIME", "SHIFT_PRODUCTION"):
        assert set(tables[name].machine_id) <= machine_ids
    assert set(tables["MAINT_NOTES"].stop_id) == set(tables["DOWNTIME"].stop_id)
    assert tables["MACHINES"].machine_id.is_unique


def test_downtime_is_consistent(tables):
    d = tables["DOWNTIME"]
    assert (d.end_ts > d.start_ts).all()
    assert set(d.stop_type) == {"PLANNED", "UNPLANNED"}
    assert d.loc[d.stop_type == "UNPLANNED", "failure_mode"].notna().all()
    assert d.loc[d.stop_type == "PLANNED", "failure_mode"].isna().all()


def test_no_telemetry_while_machine_is_down(tables):
    t = tables["TELEMETRY"].copy()
    d = tables["DOWNTIME"]
    for _, stop in d.iterrows():
        inside = t[(t.machine_id == stop.machine_id) & (t.reading_ts > stop.start_ts) & (t.reading_ts < stop.end_ts)]
        assert inside.empty


def test_oee_components_in_range(tables):
    p = tables["SHIFT_PRODUCTION"]
    run = p.planned_minutes - p.downtime_minutes
    availability = run / p.planned_minutes
    performance = (p.ideal_cycle_s * p.total_count) / (run * 60).replace(0, np.nan)
    quality = p.good_count / p.total_count.replace(0, np.nan)
    assert availability.between(0, 1).all()
    assert performance.dropna().between(0, 1).all()
    assert quality.dropna().between(0, 1).all()


def test_features_complete_and_finite(training_df):
    assert all(f in training_df.columns for f in FEATURES)
    assert np.isfinite(training_df[FEATURES].to_numpy(dtype=float)).all()


def test_label_is_plausible(training_df):
    rate = training_df[LABEL].mean()
    assert 0.02 < rate < 0.30


def test_label_matches_definition(training_df, tables):
    """Spot-check: label == unplanned stop starting in (ts, ts+24h]."""
    d = tables["DOWNTIME"]
    d = d[d.stop_type == "UNPLANNED"]
    sample = training_df.sample(300, random_state=0)
    for _, r in sample.iterrows():
        s = d[(d.machine_id == r.MACHINE_ID) & (d.start_ts > r.READING_TS)
              & (d.start_ts <= r.READING_TS + pd.Timedelta(hours=24))]
        assert int(not s.empty) == r[LABEL]
