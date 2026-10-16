"""Diagnosis views (sql/07_diagnosis.sql) on top of the root cause (sql/06_root_cause.sql)."""
import pytest


@pytest.fixture(scope="module")
def views(duck):
    status = {r["MACHINE_ID"]: r for r in duck.query("SELECT * FROM OEE.V_MACHINE_STATUS")}
    modes = {}
    for r in duck.query("SELECT * FROM OEE.V_MODE_LIKELIHOOD"):
        modes.setdefault(r["MACHINE_ID"], {})[r["FAILURE_MODE"]] = r
    rc = {}
    for r in duck.query("SELECT * FROM OEE.V_ROOT_CAUSE_LATEST"):
        rc.setdefault(r["MACHINE_ID"], {})[r["FAILURE_MODE"]] = r
    devs = {}
    for r in duck.query("SELECT * FROM OEE.V_SIGNAL_DEVIATIONS"):
        devs.setdefault(r["MACHINE_ID"], []).append(r)
    parts = {r["FAILURE_MODE"]: r["PART_NO"] for r in duck.query("SELECT * FROM OEE.FAILURE_MODES")}
    stock = {(r["PLANT_ID"], r["PART_NO"]): r["ON_HAND"] for r in duck.query("SELECT * FROM OEE.SPARE_PARTS")}
    return status, modes, rc, devs, parts, stock


def test_every_machine_is_diagnosed(views):
    status, modes, *_ = views
    assert len(status) == 24 and set(modes) == set(status)
    assert all(len(m) == 5 for m in modes.values())


def test_mode_probabilities_are_the_root_cause(views):
    _, modes, rc, *_ = views
    for mid, ms in modes.items():
        assert sum(m["PROBABILITY"] for m in ms.values()) == pytest.approx(1.0)
        for mode, m in ms.items():
            assert m["PROBABILITY"] == pytest.approx(rc[mid][mode]["CONFIDENCE"])
            assert m["MODE_RANK"] == rc[mid][mode]["CAUSE_RANK"]


def test_top_mode_part_stock_and_stage(views):
    status, modes, _, _, parts, stock = views
    for mid, s in status.items():
        top = min(modes[mid].values(), key=lambda m: m["MODE_RANK"])
        assert s["TOP_MODE"] == top["FAILURE_MODE"]
        assert s["PART_NO"] == parts[s["TOP_MODE"]]
        assert s["ON_HAND"] == stock.get((s["PLANT_ID"], s["PART_NO"]))
        r = s["RISK_24H"]
        assert s["HEALTH"] == ("critical" if r >= 0.5 else "watch" if r >= 0.10 else "healthy")
        expected = ("WORK_ORDER" if s["OPEN_WO_ID"] is not None else
                    "PARTS_BLOCKED" if s["ON_HAND"] is not None and s["ON_HAND"] <= 0 else
                    "READY" if r >= 0.10 else "MONITOR")
        assert s["STAGE"] == expected, mid


def test_signals_ranked_by_size_of_deviation(views):
    *_, devs, _, _ = views
    for mid, rows in devs.items():
        ranked = sorted(rows, key=lambda r: r["DEV_RANK"])
        z = [abs(r["Z"] or 0) for r in ranked]
        assert z == sorted(z, reverse=True), mid
