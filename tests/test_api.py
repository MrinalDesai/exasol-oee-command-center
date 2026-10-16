"""Console API checks, run against the DuckDB stand-in (tests/fakedb.py)."""
import pytest
from fastapi.testclient import TestClient

from oee.app import create_app


@pytest.fixture(scope="module")
def client(duck):
    return TestClient(create_app(lambda: duck))


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
    worn = client.post("/api/whatif", json={"machine_id": mid, "overrides": {"TOOL_WEAR_MIN": 320}}).json()
    assert "FAILURE_RISK(" in base["sql"]
    assert 0 <= fresh["rows"][0]["RISK_24H"] <= 1
    assert worn["rows"][0]["RISK_24H"] >= fresh["rows"][0]["RISK_24H"]


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


def test_story_endpoints(client):
    f = client.get("/api/fusion").json()
    assert len(f["health"]["rows"]) == 5 and len(f["impact"]["rows"]) == 2
    mid = client.get("/api/risk?limit=1").json()["rows"][0]["MACHINE_ID"]
    rc = client.get(f"/api/rootcause/{mid}").json()
    assert len(rc["patterns"]["rows"]) == 5 and len(rc["causes"]["rows"]) == 5
    assert rc["accuracy"]["rows"][0]["STOPS"] > 0
    assert client.get(f"/api/sourcing?machine={mid}").json()["plan"]["rows"]
    assert client.get("/api/quality").json()["impact"]["rows"]
    assert client.get("/api/rootcause/X';--").status_code in (400, 404)


@pytest.mark.parametrize("mode", ["BEARING", "HEAT_DISSIPATION", "OVERSTRAIN", "POWER"])
def test_demo_search_and_replay(client, mode):
    from oee import patterns

    d = client.get(f"/api/demo/search?mode={mode}").json()
    s = d["summary"]["rows"][0]
    assert d["rows_scanned"] > 0 and d["search"]["ms"] >= 0
    assert 0 < s["EPISODES"] and s["EPISODES_CAME_TRUE"] <= s["EPISODES"] and s["FAILURES_WARNED"] <= s["FAILURES"]
    assert len(d["signature"]["rows"]) == 5
    r = client.get(f"/api/demo/replay?mode={mode}").json()
    if r["stream"] is None:                          # small test plant: no named held-out failure of this kind
        assert r["pick"]["rows"] == []
        return
    rows = r["stream"]["rows"]
    own = patterns.MODES[mode][1]
    run = best = 0
    for x in rows:                                   # the replay must reach the same rule as the search
        run = run + 1 if x["TOP_MODE"] == mode and (x[own] or 0) >= patterns.ALARM_Z else 0
        best = max(best, run)
    assert best >= patterns.MIN_HOURS
    assert r["kit"]["rows"]


def test_demo_rejects_unknown_mode(client):
    assert client.get("/api/demo/search?mode=X'--").status_code == 400
    assert client.get("/demo").status_code == 200
