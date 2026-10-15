"""Console API checks, run against the DuckDB stand-in (tests/fakedb.py)."""
import pytest
from fastapi.testclient import TestClient

from oee.app import create_app
from tests.fakedb import DuckBackend


@pytest.fixture(scope="module")
def client():
    backend = DuckBackend(days=30, seed=42)
    return TestClient(create_app(lambda: backend))


def test_index_and_static(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_every_response_carries_sql_and_timing(client):
    for url in ["/api/health", "/api/model", "/api/oee/losses", "/api/oee/daily", "/api/risk", "/api/parts", "/api/pareto"]:
        body = client.get(url).json()
        assert body["rows"], url
        assert body["sql"].upper().startswith("SELECT"), url
        assert body["ms"] >= 0, url


@pytest.mark.parametrize("query", ["level=plant", "level=line&plant=CHN", "level=machine&line=CHN-L2"])
def test_losses_add_up_to_planned_time(client, query):
    for r in client.get("/api/oee/losses?" + query).json()["rows"]:
        parts = r["PRODUCTIVE_MIN"] + r["AVAILABILITY_LOSS_MIN"] + r["PERFORMANCE_LOSS_MIN"] + r["QUALITY_LOSS_MIN"]
        assert parts == pytest.approx(r["PLANNED_MIN"], rel=1e-9)
        assert r["OEE"] == pytest.approx(r["AVAILABILITY"] * r["PERFORMANCE"] * r["QUALITY"], rel=1e-9)


def test_planted_story_is_visible(client):
    plants = client.get("/api/oee/losses").json()["rows"]
    assert plants[0]["UNIT_ID"] == "CHN"  # worst first
    lines = client.get("/api/oee/losses?level=line&plant=CHN").json()["rows"]
    assert lines[0]["UNIT_ID"] == "CHN-L2"
    assert lines[0]["PERFORMANCE"] < 0.8


def test_machine_drilldown(client):
    mid = client.get("/api/risk?limit=1").json()["rows"][0]["MACHINE_ID"]
    body = client.get(f"/api/machine/{mid}").json()
    assert body["info"]["rows"][0]["MACHINE_ID"] == mid
    assert len(body["series"]["rows"]) > 100
    assert client.get("/api/machine/PNQ-MILL-99").status_code == 404


def test_whatif_scores_through_the_udf(client):
    mid = client.get("/api/risk?limit=1").json()["rows"][0]["MACHINE_ID"]
    base = client.post("/api/whatif", json={"machine_id": mid, "overrides": {}}).json()
    fresh = client.post("/api/whatif", json={"machine_id": mid, "overrides": {"TOOL_WEAR_MIN": 0}}).json()
    assert "FAILURE_RISK(" in base["sql"]
    assert 0 <= fresh["rows"][0]["RISK_24H"] <= 1
    assert fresh["rows"][0]["RISK_24H"] <= base["rows"][0]["RISK_24H"]


def test_whatif_rejects_unknown_features(client):
    r = client.post("/api/whatif", json={"machine_id": "CHN-LATHE-06", "overrides": {"RPM": 1}})
    assert r.status_code == 400


@pytest.mark.parametrize("url", [
    "/api/machine/CHN-LATHE-06';DROP TABLE OEE.MACHINES;--",
    "/api/oee/losses?level=line&plant=CHN'--",
    "/api/oee/losses?level=evil",
    "/api/pareto?plant=X' OR '1'='1",
])
def test_inputs_are_validated(client, url):
    assert client.get(url).status_code in (400, 404)


def test_fleet_assessment(client):
    machines = client.get("/api/fleet").json()["machines"]
    assert len(machines) == 24
    for m in machines:
        assert m["status"] in {"healthy", "watch", "critical"}
        assert sum(x["p"] for x in m["likely_modes"]) == pytest.approx(1.0)
        assert set(m["signals"]) == {"TOOL_WEAR_MIN", "VIBRATION_MM_S", "TEMP_DIFF_K", "POWER_KW", "TORQUE_RATIO"}


def test_diagnosis_and_history(client):
    mid = max(client.get("/api/fleet").json()["machines"], key=lambda m: m["risk"])["machine_id"]
    d = client.get(f"/api/diagnosis/{mid}").json()
    assert d["machine"]["machine_id"] == mid
    top = d["machine"]["likely_modes"][0]["mode"]
    assert all(r["FAILURE_MODE"] == top for r in d["similar"]["rows"])
    assert d["parts"]["rows"]
    h = client.get(f"/api/history/{top}").json()
    assert h["rows"] and all(r["NOTE_TEXT"] for r in h["rows"])
    assert client.get("/api/history/NOT_A_MODE").status_code == 400


def test_work_order_lifecycle(client):
    mid = max(client.get("/api/fleet").json()["machines"], key=lambda m: m["risk"])["machine_id"]
    r = client.post("/api/workorders", json={"machine_id": mid})
    assert r.status_code == 200 and r.json()["sql"].startswith("INSERT INTO OEE.WORK_ORDERS")
    assert client.post("/api/workorders", json={"machine_id": mid}).status_code == 409
    m = next(x for x in client.get("/api/fleet").json()["machines"] if x["machine_id"] == mid)
    assert m["stage"] == "WORK_ORDER"
    client.post(f"/api/workorders/{r.json()['wo_id']}/close")
    assert all(w["MACHINE_ID"] != mid for w in client.get("/api/workorders").json()["rows"])
