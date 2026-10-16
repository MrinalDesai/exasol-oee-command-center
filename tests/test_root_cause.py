"""Step 2: per-stream pattern detectors and fault signatures (sql/06_root_cause.sql)."""
import pytest

MODES = {"TOOL_WEAR", "HEAT_DISSIPATION", "POWER", "OVERSTRAIN", "BEARING"}


def test_five_detectors_per_machine_hour(duck):
    rows = duck.query("""SELECT MACHINE_ID, READING_TS, COUNT(*) AS N, COUNT(DISTINCT STREAM) AS S
                         FROM OEE.STREAM_PATTERNS GROUP BY MACHINE_ID, READING_TS""")
    assert rows and all(r["N"] == 5 and r["S"] == 5 for r in rows)
    n_feat = duck.query("SELECT COUNT(*) AS N FROM OEE.V_FEATURES")[0]["N"]
    assert len(rows) == n_feat


def test_signatures_learned_only_before_cutoff(duck):
    sig = duck.query("SELECT * FROM OEE.FAULT_SIGNATURES")
    assert {r["FAILURE_MODE"] for r in sig} <= MODES
    n_before = duck.query("""SELECT COUNT(*) AS N FROM OEE.DOWNTIME d, (SELECT MAX(TRAIN_CUTOFF) AS TS FROM OEE.MODEL_REGISTRY WHERE MODEL_NAME = 'FAILURE_RISK') c
                             WHERE d.STOP_TYPE = 'UNPLANNED' AND d.START_TS <= c.TS""")[0]["N"]
    learned = sum({r["FAILURE_MODE"]: r["LEARNED_FROM_STOPS"] for r in sig}.values())
    assert 0 < learned <= n_before


def test_each_fault_lights_its_own_stream_and_signatures_differ(duck):
    sig = {}
    for r in duck.query("SELECT * FROM OEE.FAULT_SIGNATURES"):
        sig.setdefault(r["FAILURE_MODE"], {})[r["STREAM"]] = r["SIGNATURE_Z"]
    own = {"BEARING": "VIBRATION", "HEAT_DISSIPATION": "TEMPERATURE", "OVERSTRAIN": "TORQUE",
           "POWER": "POWER", "TOOL_WEAR": "TOOL"}
    for mode, stream in own.items():
        assert sig[mode][stream] >= 1.0, (mode, sig[mode])
    # overstrain also lifts power (power = torque x speed); the combination tells them apart
    names = sorted(sig)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            d2 = sum((sig[a][k] - sig[b][k]) ** 2 for k in sig[a])
            assert d2 > 1.0, (a, b)


def test_root_cause_is_a_distribution(duck):
    rows = duck.query("""SELECT MACHINE_ID, READING_TS, SUM(CONFIDENCE) AS P, MIN(CAUSE_RANK) AS R1, MAX(CAUSE_RANK) AS R5
                         FROM OEE.ROOT_CAUSE GROUP BY MACHINE_ID, READING_TS""")
    assert all(r["P"] == pytest.approx(1.0) and r["R1"] == 1 and r["R5"] == 5 for r in rows)


def test_root_cause_beats_majority_baseline_on_held_out_stops(duck):
    acc = duck.query("SELECT * FROM OEE.V_ROOT_CAUSE_ACCURACY")[0]
    assert acc["STOPS"] >= 5
    assert acc["ACCURACY"] > acc["MAJORITY_BASELINE"]
    assert acc["TOP2_ACCURACY"] >= acc["ACCURACY"]


def test_latest_views_one_reading_per_machine(duck):
    pats = duck.query("SELECT MACHINE_ID, COUNT(*) AS N FROM OEE.V_STREAM_PATTERNS_LATEST GROUP BY MACHINE_ID")
    assert len(pats) == 24 and all(r["N"] == 5 for r in pats)
