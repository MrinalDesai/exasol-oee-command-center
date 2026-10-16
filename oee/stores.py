"""Spare-parts stock as it really sits: one inventory system per store, each with
its own layout. Exasol reads all four (sql/03_stores.sql) into OEE.SPARE_PARTS.

  SAP_PNQ.MARD             Pune plant store, SAP-style        WERKS, MATNR, LABST, EISBE, PLIFZ, VERPR
  SAP_BLR.MARD             Bengaluru plant store, SAP-style   same layout, its own instance
  TALLY_CHN.STOCK_SUMMARY  Chennai plant store, Tally-style   ITEM_NAME (lower case), CLOSING_QTY, ...
  WMS_HYD.INVENTORY        Hyderabad warehouse, WMS           SKU (underscores), BIN_LOCATION, QTY_AVAILABLE, ...
"""
from __future__ import annotations

import pandas as pd

# location -> (qualified table, source system name, part-number column, how that system writes our part number)
STORE_SYSTEMS = {
    "PNQ": ("SAP_PNQ.MARD", "SAP (Pune plant)", "MATNR", "as is"),
    "BLR": ("SAP_BLR.MARD", "SAP (Bengaluru plant)", "MATNR", "as is"),
    "CHN": ("TALLY_CHN.STOCK_SUMMARY", "Tally (Chennai plant)", "ITEM_NAME", "lower case"),
    "HYD": ("WMS_HYD.INVENTORY", "WMS (Hyderabad warehouse)", "SKU", "underscores"),
}


def split_stores(parts: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """SPARE_PARTS (plant_id, part_no, on_hand, reorder_point, lead_time_days, unit_cost_inr)
    -> one table per store system, in that system's own layout."""
    out = {}
    for loc in ("PNQ", "BLR"):
        p = parts[parts.plant_id == loc]
        out[STORE_SYSTEMS[loc][0]] = pd.DataFrame({
            "werks": p.plant_id, "matnr": p.part_no, "labst": p.on_hand, "eisbe": p.reorder_point,
            "plifz": p.lead_time_days, "verpr": p.unit_cost_inr})
    p = parts[parts.plant_id == "CHN"]
    out[STORE_SYSTEMS["CHN"][0]] = pd.DataFrame({
        "item_name": p.part_no.str.lower(), "closing_qty": p.on_hand, "reorder_level": p.reorder_point,
        "lead_days": p.lead_time_days, "rate": p.unit_cost_inr})
    p = parts[parts.plant_id == "HYD"].reset_index(drop=True)
    out[STORE_SYSTEMS["HYD"][0]] = pd.DataFrame({
        "sku": p.part_no.str.replace("-", "_"), "bin_location": [f"A-{3 + i:02d}-{1 + i % 4}" for i in range(len(p))],
        "qty_available": p.on_hand, "min_qty": p.reorder_point, "lead_time_d": p.lead_time_days,
        "unit_cost": p.unit_cost_inr})
    return out
