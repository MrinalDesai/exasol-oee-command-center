"""Step 5: suppliers reporting in inconsistent formats (sql/11_suppliers.sql)."""
import json

import pytest


def test_three_formats_read_into_one_offer_list(duck):
    offers = duck.query("SELECT * FROM OEE.V_SUPPLIER_OFFERS")
    t = duck.tables
    assert len(offers) == len(t["SRC_SUPPLIER_A"]) + len(t["SRC_SUPPLIER_B"]) + len(t["SRC_SUPPLIER_C"])
    assert all(o["UNIT_PRICE_INR"] and o["UNIT_PRICE_INR"] > 0 and o["LEAD_DAYS"] >= 1 for o in offers)


def test_units_are_converted(duck):
    offers = {(o["SUPPLIER_ID"], o["SUPPLIER_CODE"]): o for o in duck.query("SELECT * FROM OEE.V_SUPPLIER_OFFERS")}
    raw = json.loads(duck.tables["SRC_SUPPLIER_B"].raw_json.iloc[0])
    b = offers[("B", raw["sku"])]
    assert b["UNIT_PRICE_INR"] == pytest.approx(raw["price"]["usd"] * 83.0)
    assert b["LEAD_DAYS"] == raw["lead_time_weeks"] * 7
    for _, c in duck.tables["SRC_SUPPLIER_C"].iterrows():
        o = offers[("C", c.partnumber)]
        assert o["UNIT_PRICE_INR"] == pytest.approx(float(c.rate.replace("Rs. ", "").replace(",", "")))
        n = int(c.delivery.split()[0])
        assert o["LEAD_DAYS"] == (n * 7 if "wk" in c.delivery else n)


def test_exact_matches_are_all_correct_and_near_misses_rejected(duck):
    acc = duck.query("SELECT * FROM OEE.V_MATCH_ACCURACY")[0]
    assert acc["AUTO_PRECISION"] == 1.0
    assert acc["AUTO_RECALL"] >= 0.85
    matches = {(m["SUPPLIER_ID"], m["DESCRIPTION"]): m for m in duck.query("SELECT * FROM OEE.SUPPLIER_MATCHES")}
    assert ("A", "BRG 6204-2RS DEEP GROOVE") not in matches          # a different bearing
    assert matches[("C", "Seal 45*62*8")]["PART_NO"] == "GEARBOX-SEAL-GS7"
    assert matches[("B", "Kalmar CNMG 12 04 08-PM Turning Insert")]["MATCH_TYPE"] == "EXACT"


def test_near_codes_go_to_review_not_to_orders(duck):
    review = {m["DESCRIPTION"]: m for m in duck.query("SELECT * FROM OEE.SUPPLIER_MATCHES WHERE MATCH_TYPE = 'REVIEW'")}
    assert "Carbide Insert CNMG120480" in review                      # supplier typo
    assert "INSERT CNMG-120404 CARBIDE" in review                     # a different insert
    bom_codes = {b["SUPPLIER_CODE"] for b in duck.query("SELECT * FROM OEE.V_BOM_TO_ORDER")}
    assert not bom_codes & {m["SUPPLIER_CODE"] for m in review.values()}


def test_bom_quantities_respect_order_multiples(duck):
    for b in duck.query("SELECT * FROM OEE.V_BOM_TO_ORDER"):
        need = b["URGENT_QTY"] + b["REPLENISH_QTY"]
        assert b["ORDER_QTY"] >= need > 0
        if b["SUPPLIER_ID"] is not None:
            assert b["LINE_TOTAL_INR"] == pytest.approx(b["ORDER_QTY"] * b["UNIT_PRICE_INR"])


def test_api(duck):
    from fastapi.testclient import TestClient

    from oee.app import create_app

    r = TestClient(create_app(lambda: duck)).get("/api/suppliers").json()
    assert r["accuracy"]["rows"][0]["AUTO_PRECISION"] == 1.0 and r["offers"]["rows"]


@pytest.mark.parametrize("part", ["BEARING-6205", "TOOL-INSERT-T12", "GEARBOX-SEAL-GS7"])
def test_demo_order_plain_search_misses_deep_scan_finds(duck, part):
    from fastapi.testclient import TestClient

    from oee.app import create_app

    c = TestClient(create_app(lambda: duck))
    d = c.get(f"/api/demo/order?part={part}&qty=2").json()
    assert d["plain"]["rows"] == []                                   # nobody uses our part number
    same = [r for r in d["deep"]["rows"] if r["VERDICT"] == "SAME"]
    assert len({r["SUPPLIER_ID"] for r in same}) >= 2                # found at several suppliers
    truth = {(r["SUPPLIER_ID"], r["SUPPLIER_CODE"]): r["TRUE_PART_NO"]
             for r in duck.query("SELECT * FROM OEE.SUPPLIER_MATCH_AUDIT")}
    assert all(truth[(r["SUPPLIER_ID"], r["SUPPLIER_CODE"])] == part for r in same)
    assert all(r["RAW_LINE"] for r in d["deep"]["rows"])
    assert c.get("/api/demo/order?part=NOPE").status_code == 400
