"""Step 3: spare parts held in the wrong location (sql/09_sourcing.sql)."""
import pytest
from fastapi.testclient import TestClient

from oee.app import create_app


@pytest.fixture(scope="module")
def client(duck):
    return TestClient(create_app(lambda: duck))


def test_every_kit_part_gets_one_best_source(duck):
    best = duck.query("SELECT * FROM OEE.V_SOURCING_BEST")
    kits = duck.query("SELECT COUNT(*) AS N FROM OEE.REPAIR_KITS")[0]["N"]
    plants = duck.query("SELECT COUNT(*) AS N FROM OEE.LOCATIONS WHERE KIND = 'PLANT'")[0]["N"]
    assert len(best) == kits * plants
    assert len({(r["PLANT_ID"], r["FAILURE_MODE"], r["PART_NO"]) for r in best}) == len(best)


def test_best_source_is_fastest_feasible(duck):
    opts = duck.query("SELECT * FROM OEE.V_SOURCING_OPTIONS")
    by = {}
    for o in opts:
        by.setdefault((o["PLANT_ID"], o["FAILURE_MODE"], o["PART_NO"]), []).append(o)
    for key, os_ in by.items():
        first = min(os_, key=lambda o: o["OPTION_RANK"])
        assert first["ETA_HOURS"] == min(o["ETA_HOURS"] for o in os_), key
        for o in os_:
            if o["SOURCE_KIND"] != "SUPPLIER":
                assert o["AVAILABLE"] >= o["QTY"]          # never offers stock that isn't there
            if o["SOURCE_KIND"] == "LOCAL":
                assert o["FROM_LOCATION"] == o["PLANT_ID"]


def test_network_never_slower_than_local_only(duck):
    for r in duck.query("SELECT * FROM OEE.V_KIT_COVERAGE"):
        assert r["KIT_READY_HOURS"] <= r["LOCAL_ONLY_HOURS"]
        assert r["HOURS_SAVED"] >= 0
        assert r["PARTS_LOCAL"] + r["PARTS_TRANSFER"] + r["PARTS_SUPPLIER"] == r["KIT_PARTS"]


def test_stock_is_somewhere_else_often_enough_to_matter(duck):
    rows = duck.query("SELECT * FROM OEE.V_KIT_COVERAGE")
    assert any(r["PARTS_TRANSFER"] > 0 for r in rows)
    assert sum(r["HOURS_SAVED"] for r in rows) > 0


def test_work_order_reserves_kit_and_close_releases(client, duck):
    fleet = duck.query("SELECT MACHINE_ID FROM OEE.V_MACHINE_STATUS WHERE OPEN_WO_ID IS NULL ORDER BY RISK_24H DESC")
    mid = fleet[0]["MACHINE_ID"]
    plan = client.get(f"/api/sourcing?machine={mid}").json()
    parts = {r["PART_NO"]: r for r in plan["plan"]["rows"]}
    assert parts and plan["options"]["rows"]
    before = {(r["LOCATION_ID"], r["PART_NO"]): r["AVAILABLE"] for r in duck.query("SELECT * FROM OEE.V_PART_AVAILABLE")}

    wo = client.post("/api/workorders", json={"machine_id": mid}).json()
    res = [r for r in client.get("/api/sourcing").json()["reservations"]["rows"] if r["WO_ID"] == wo["wo_id"]]
    assert {r["PART_NO"] for r in res} == set(parts)
    after = {(r["LOCATION_ID"], r["PART_NO"]): r["AVAILABLE"] for r in duck.query("SELECT * FROM OEE.V_PART_AVAILABLE")}
    for r in res:
        if r["FROM_LOCATION"] is not None:
            assert after[(r["FROM_LOCATION"], r["PART_NO"])] == before[(r["FROM_LOCATION"], r["PART_NO"])] - r["QTY"]

    client.post(f"/api/workorders/{wo['wo_id']}/close")
    again = {(r["LOCATION_ID"], r["PART_NO"]): r["AVAILABLE"] for r in duck.query("SELECT * FROM OEE.V_PART_AVAILABLE")}
    assert again == before


def test_four_store_systems_read_into_spare_parts(duck):
    from oee.stores import STORE_SYSTEMS

    for loc, (table, *_rest) in STORE_SYSTEMS.items():
        n = duck.query(f"SELECT COUNT(*) AS N FROM {table}")[0]["N"]
        assert n == duck.query(f"SELECT COUNT(*) AS N FROM OEE.SPARE_PARTS WHERE PLANT_ID = '{loc}'")[0]["N"] > 0
    kits = {r["PART_NO"] for r in duck.query("SELECT PART_NO FROM OEE.REPAIR_KITS")}
    assert {r["PART_NO"] for r in duck.query("SELECT DISTINCT PART_NO FROM OEE.SPARE_PARTS")} == kits


@pytest.mark.parametrize("mode", ["BEARING", "HEAT_DISSIPATION", "OVERSTRAIN", "POWER"])
def test_demo_kit_search(client, duck, mode):
    plant = "BLR"
    d = client.get(f"/api/demo/parts?mode={mode}&plant={plant}").json()
    kit = {r["PART_NO"]: r["QTY"] for r in duck.query(f"SELECT * FROM OEE.REPAIR_KITS WHERE FAILURE_MODE = '{mode}'")}
    local = {r["PART_NO"]: r for r in d["local"]["rows"]}
    assert set(local) == set(kit)
    for p, r in local.items():
        expect = "FOUND" if r["FREE_QTY"] >= kit[p] else "SHORT" if r["FREE_QTY"] > 0 else "MISSING"
        assert r["LOCAL_STATUS"] == expect
    best = {r["PART_NO"]: r for r in d["network"]["rows"] if r["OPTION_RANK"] == 1}
    assert set(best) == set(kit)
    for p, b in best.items():
        assert b["CAN_SUPPLY"] == 1
        if local[p]["LOCAL_STATUS"] == "FOUND":
            assert b["SOURCE_KIND"] == "LOCAL"
    assert client.get(f"/api/demo/parts?mode={mode}&plant=HYD").status_code == 400
