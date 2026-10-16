"""Scale benchmark: generator output, fusion SQL and the alert/validation queries,
run on DuckDB with the vectorised scorer standing in for the Exasol SET UDF."""
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from oee import vscorer
from oee.compile_udf import booster_to_spec
from oee.features import FEATURES, add_labels, build_features
from oee.generate import generate
from oee.scale import ALERTS_SQL, VALIDATION_SQL, fusion_sql, generate_scale
from oee.train import train
from tests.fakedb import translate


@pytest.fixture(scope="module")
def scale_db(tmp_path_factory):
    out = tmp_path_factory.mktemp("scale")
    info = generate_scale(out, machines=9, days=40, cadence_min=1, workers=1, chunk_machines=4)
    con = duckdb.connect()
    con.execute("CREATE SCHEMA OEE_SCALE")
    con.execute("CREATE TABLE OEE_SCALE.MACHINES AS SELECT * FROM read_csv_auto(?)", [str(out / "machines.csv")])
    con.execute("CREATE TABLE OEE_SCALE.DOWNTIME AS SELECT * FROM read_csv_auto(?)", [str(out / "downtime.csv")])
    cols = {"MACHINE_ID": "VARCHAR", "READING_TS": "TIMESTAMP", "AIR_TEMP_K": "DOUBLE", "PROCESS_TEMP_K": "DOUBLE",
            "RPM": "DOUBLE", "TORQUE_NM": "DOUBLE", "TOOL_WEAR_MIN": "DOUBLE", "VIBRATION_MM_S": "DOUBLE", "POWER_KW": "DOUBLE"}
    files = [str(f) for f in sorted(out.glob("telemetry_*.csv"))]
    con.execute("CREATE TABLE OEE_SCALE.TELEMETRY AS SELECT * FROM read_csv(?, header=false, columns=?)", [files, cols])
    con.execute(translate(fusion_sql(1)))
    # model trained on the hourly demo plant, as in the real benchmark
    t = generate(days=60, seed=42, per_plant=8)
    model, _ = train(add_labels(build_features(t["TELEMETRY"], t["MACHINES"]), t["DOWNTIME"]), n_estimators=120)
    vm = vscorer.vload(booster_to_spec(model.get_booster()))
    feats = con.execute(f"SELECT MACHINE_ID, READING_TS, {', '.join(FEATURES)} FROM OEE_SCALE.FEATURES").df()
    feats["RISK_24H"] = vscorer.vproba(vm, feats[FEATURES].to_numpy())
    scores = feats[["MACHINE_ID", "READING_TS", "RISK_24H"]]
    con.register("scores_df", scores)
    con.execute("CREATE TABLE OEE_SCALE.SCORES AS SELECT * FROM scores_df")
    return con, info, out


def test_generator_sizes(scale_db):
    con, info, out = scale_db
    assert info["machines"] == 9 and info["files"] == 3
    n = con.execute("SELECT COUNT(*) FROM OEE_SCALE.TELEMETRY").fetchone()[0]
    assert n == info["rows"] > 9 * 40 * 24 * 50       # mostly running, one reading a minute
    gaps = con.execute("""SELECT COUNT(*) FROM (SELECT MACHINE_ID, READING_TS,
        READING_TS - LAG(READING_TS) OVER (PARTITION BY MACHINE_ID ORDER BY READING_TS) AS d FROM OEE_SCALE.TELEMETRY)
        WHERE d < INTERVAL 1 MINUTE""").fetchone()[0]
    assert gaps == 0                                    # no duplicate timestamps


def test_no_readings_while_machine_is_down(scale_db):
    con, _, _ = scale_db
    inside = con.execute("""SELECT COUNT(*) FROM OEE_SCALE.TELEMETRY t JOIN OEE_SCALE.DOWNTIME d
        ON d.MACHINE_ID = t.MACHINE_ID AND t.READING_TS > d.START_TS + INTERVAL 1 HOUR AND t.READING_TS < d.END_TS""").fetchone()[0]
    assert inside == 0


def test_fusion_windows_are_six_hours(scale_db):
    con, _, _ = scale_db
    df = con.execute("""SELECT f.READING_TS, f.VIB_AVG_6H, t.VIBRATION_MM_S FROM OEE_SCALE.FEATURES f
        JOIN OEE_SCALE.TELEMETRY t USING (MACHINE_ID, READING_TS)
        WHERE MACHINE_ID = (SELECT MIN(MACHINE_ID) FROM OEE_SCALE.MACHINES) ORDER BY READING_TS""").df()
    expected = df["VIBRATION_MM_S"].rolling(360, min_periods=1).mean()
    assert np.allclose(df["VIB_AVG_6H"], expected)
    assert con.execute("SELECT COUNT(*) FROM OEE_SCALE.FEATURES WHERE VIB_AVG_6H IS NULL OR TORQUE_RATIO IS NULL").fetchone()[0] == 0


def test_alerts_and_validation_queries(scale_db):
    con, _, _ = scale_db
    machines, critical, watch, max_risk = con.execute(translate(ALERTS_SQL)).fetchone()
    assert machines == 9 and 0 <= critical <= watch <= 9 and 0 < max_risk <= 1
    readings, followed, alerts, hits = con.execute(translate(VALIDATION_SQL)).fetchone()
    assert readings == con.execute("SELECT COUNT(*) FROM OEE_SCALE.SCORES").fetchone()[0]
    assert alerts > 0
    base, precision = followed / readings, hits / alerts
    # the hourly-trained model must still find real patterns in one-minute data
    assert precision > 2 * base, (precision, base)
