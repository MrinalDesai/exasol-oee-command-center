"""Step 1: Exasol combines five scattered sources back into one record per machine."""
import pandas as pd
import pytest


@pytest.fixture(scope="module")
def fused_vs_truth(duck):
    fused = duck.con.execute("SELECT * FROM oee.TELEMETRY").df()
    truth = duck.tables["TELEMETRY_TRUTH"].copy()
    truth.columns = [c.upper() for c in truth.columns]
    return fused.merge(truth, on=["MACHINE_ID", "READING_TS"], suffixes=("_F", "_T")), fused, truth


def test_sources_really_are_scattered(duck):
    reg = duck.tables["ASSET_REGISTRY"]
    # every system uses its own identifier for the same machine
    for col in ["ASSET_TAG", "CM_SENSOR_ID", "PLC_TAG", "DRIVE_ID", "METER_ID"]:
        assert not set(reg[col]) & set(reg["MACHINE_ID"])
    plc = duck.tables["SRC_PLC_TEMPS"]
    assert plc["AIR_C"].between(10, 40).all()                       # Celsius, not Kelvin
    assert (pd.to_datetime(plc["TS_LOCAL"]).dt.minute % 5 == 0).all()  # 5-minute cadence
    energy = duck.tables["SRC_ENERGY"]
    delay = (pd.to_datetime(energy.RECEIVED_AT_UTC) - pd.to_datetime(energy.INTERVAL_START_UTC)).dt.total_seconds() / 60
    assert delay.median() > 60                                      # arrives late


def test_fusion_recovers_almost_every_operating_hour(fused_vs_truth):
    joined, fused, truth = fused_vs_truth
    assert len(joined) >= 0.99 * len(truth)
    assert len(fused) == len(joined)                                 # no invented hours


@pytest.mark.parametrize("col,tolerance", [
    ("AIR_TEMP_K", 0.5), ("PROCESS_TEMP_K", 0.8), ("TOOL_WEAR_MIN", 1.0),
    ("VIBRATION_MM_S", 0.15), ("TORQUE_NM", 4.0), ("POWER_KW", 0.8),
])
def test_fused_values_match_the_truth(fused_vs_truth, col, tolerance):
    joined, _, _ = fused_vs_truth
    err = (joined[col + "_F"] - joined[col + "_T"]).abs().mean()
    assert err < tolerance, f"{col}: mean abs error {err:.3f}"


def test_time_zone_is_corrected(fused_vs_truth):
    """If local time were read as UTC, temperatures would lag the truth by 5.5 h."""
    joined, _, _ = fused_vs_truth
    aligned = (joined.PROCESS_TEMP_K_F - joined.PROCESS_TEMP_K_T).abs().mean()
    shifted = joined.sort_values(["MACHINE_ID", "READING_TS"]).groupby("MACHINE_ID")
    lagged = (shifted.PROCESS_TEMP_K_F.shift(-5) - joined.sort_values(["MACHINE_ID", "READING_TS"]).PROCESS_TEMP_K_T).abs().mean()
    assert aligned < lagged


def test_every_value_is_flagged_measured_or_filled(duck):
    q = duck.query("SELECT * FROM OEE.V_SOURCE_HEALTH ORDER BY ORD")
    assert [r["STREAM"] for r in q] == ["Vibration", "Temperatures", "Speed and torque", "Power", "Tool wear"]
    for r in q:
        assert r["MEASURED"] + r["FILLED"] + r["MISSING"] == r["GRID"]
        assert r["COMPLETE_SHARE"] > 0.97
    sparse = {r["STREAM"]: r["MEASURED_SHARE"] for r in q}
    assert sparse["Temperatures"] < 0.25 and sparse["Tool wear"] < 0.15   # completion did real work


def test_fusion_impact_compares_both_pipelines_on_held_out_stops(duck):
    rows = duck.query("SELECT * FROM OEE.FUSION_IMPACT ORDER BY ORD")
    assert [r["PIPELINE"] for r in rows] == ["Raw sources, joined as delivered", "Combined and completed in Exasol"]
    assert rows[0]["STOPS"] == rows[1]["STOPS"] > 0
    assert rows[1]["HOURS_SCORED"] >= rows[0]["HOURS_SCORED"]


def test_demo_sources_and_combine(duck):
    from fastapi.testclient import TestClient

    from oee.app import create_app

    c = TestClient(create_app(lambda: duck))
    s = c.get("/api/demo/sources").json()
    assert s["registry"]["rows"][0]["MACHINE_ID"] == s["machine"]
    assert s["vibration"]["rows"] and s["drive"]["rows"] and s["temps"]["rows"]
    d = c.post("/api/demo/combine").json()
    # the demo copy is the same combine-and-complete as the pipeline's own table
    assert d["minutes"] == duck.query("SELECT COUNT(*) AS N FROM OEE.TELEMETRY_FUSED")[0]["N"]
    rows = d["fused"]["rows"]
    assert len(rows) == 12
    ref = {r["READING_TS"]: r for r in duck.query(
        f"SELECT * FROM OEE.TELEMETRY_FUSED WHERE MACHINE_ID = '{d['machine']}'")}
    for r in rows:
        assert r["VIBRATION_MM_S"] == ref[r["READING_TS"]]["VIBRATION_MM_S"]
        assert r["TMP_Q"] == ref[r["READING_TS"]]["TMP_Q"]
