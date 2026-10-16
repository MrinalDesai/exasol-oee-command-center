"""Step 4: quality problems predicted before the shift (sql/04_quality_features.sql, 10_quality.sql)."""
import pytest

from oee.quality import QUALITY_FEATURES


def test_features_come_from_before_the_shift(duck):
    rows = duck.query("SELECT SHIFT_START, READING_TS FROM OEE.V_QUALITY_TRAINING")
    assert rows and all(r["READING_TS"] < r["SHIFT_START"] for r in rows)
    cols = duck.query("SELECT * FROM OEE.V_QUALITY_TRAINING LIMIT 1")[0]
    assert set(QUALITY_FEATURES) <= set(cols)


def test_one_row_per_shift(duck):
    r = duck.query("""SELECT COUNT(*) AS N, COUNT(DISTINCT MACHINE_ID || CAST(SHIFT_START AS VARCHAR)) AS D
                      FROM OEE.V_QUALITY_TRAINING""")[0]
    assert r["N"] == r["D"]


def test_udf_matches_trained_model(duck):
    import pandas as pd

    df = pd.DataFrame(duck.query("SELECT * FROM OEE.QUALITY_SCORES ORDER BY MACHINE_ID, SHIFT_START LIMIT 200"))
    feats = pd.DataFrame(duck.query(
        "SELECT * FROM OEE.V_QUALITY_TRAINING ORDER BY MACHINE_ID, SHIFT_START LIMIT 200"))
    p = duck.qmodel.predict_proba(feats[QUALITY_FEATURES].astype(float))[:, 1]
    assert df["QUALITY_RISK"].to_numpy() == pytest.approx(p, abs=1e-4)


def test_model_finds_new_problems_inspection_cannot(duck):
    rows = {r["ORD"]: r for r in duck.query("SELECT * FROM OEE.QUALITY_IMPACT")}
    model, rule = rows[1], rows[2]
    assert rule["NEW_PROBLEMS_FLAGGED"] == 0            # by definition: the previous shift was fine
    assert model["NEW_PROBLEMS_FLAGGED"] > 0
    assert model["FLAG_PRECISION"] > model["BASE_RATE"]


def test_next_shift_recommendation(client_q):
    rows = client_q.get("/api/quality").json()["next_shift"]["rows"]
    assert len(rows) == 24
    for r in rows:
        exp = "HOLD" if r["QUALITY_RISK"] >= 0.5 else "CHECK" if r["QUALITY_RISK"] >= 0.3 else "RUN"
        assert r["RECOMMENDATION"] == exp


@pytest.fixture(scope="module")
def client_q(duck):
    from fastapi.testclient import TestClient

    from oee.app import create_app

    return TestClient(create_app(lambda: duck))


def test_demo_quality_check(client_q, duck):
    d = client_q.get("/api/demo/quality").json()
    assert d["shifts_scored"] == duck.query("SELECT COUNT(*) AS N FROM OEE.V_QUALITY_TRAINING")[0]["N"]
    assert len(d["next_shift"]["rows"]) == 24 and d["score"]["ms"] >= 0
    for e in d["example"]["rows"]:
        assert e["BAD_PARTS"] == e["TOTAL_COUNT"] - e["GOOD_COUNT"] and e["QUALITY_RISK"] >= 0.5
        assert e["PREV_SCRAP_RATE"] <= 0.07 < e["SCRAP_RATE"]
