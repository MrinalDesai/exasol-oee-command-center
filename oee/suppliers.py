"""Step 5: suppliers that describe the same part in different ways and formats.

Three synthetic supplier catalogs, each in its own format:
  A  Deccan Bearings & Tools (Pune)      CSV, INR, lead time in days, MOQ
  B  Global Industrial Supply            JSON lines, USD, lead time in weeks, pack quantity
  C  Coromandel Drives & Spares (Chennai) CSV with other column names, "Rs. 1,234.50" prices,
                                          delivery as free text ("10 days", "3 wks")
None of them uses our part numbers. Each catalog also lists near-miss items
(a 6204 bearing next to our 6205, a CNMG120404 insert next to our CNMG120408)
and unrelated stock, plus a typo and a couple of descriptions with no usable
code, so matching is not trivially perfect.

SUPPLIER_MATCH_AUDIT records the true mapping for every catalog line (in a real
plant, a buyer-verified sample); Exasol uses it only to score the matching.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

# Our part master: the manufacturer code suppliers quote, and the item category
PART_MASTER = [
    # part_no, description, category, match_key (normalised manufacturer code), base price INR
    ("TOOL-INSERT-T12", "Carbide turning insert CNMG 120408", "INSERT", "CNMG120408", 650),
    ("COOLANT-PUMP-CP4", "Coolant pump 0.37 kW, CP4-370", "PUMP", "CP4370", 18500),
    ("COOLANT-FILTER-CF1", "Coolant filter cartridge CF1-25", "FILTER", "CF125", 900),
    ("SPINDLE-DRIVE-SD2", "Spindle servo drive SD2 7.5 kW", "DRIVE", "SD275", 92000),
    ("DRIVE-FUSE-F30", "Semiconductor fuse 30 A / 690 V, F30-690", "FUSE", "F30690", 1400),
    ("GEARBOX-SEAL-GS7", "Gearbox oil seal 45x62x8 NBR", "SEAL", "45X62X8", 380),
    ("GEAR-OIL-GO5", "Industrial gear oil ISO VG 220, 5 L", "OIL", "VG220", 2600),
    ("BEARING-6205", "Deep groove ball bearing 6205 2RS", "BEARING", "6205", 420),
    ("BEARING-GREASE-BG1", "Lithium bearing grease EP2, 1 kg", "GREASE", "EP2", 650),
]
CATEGORY_TERMS = {
    "INSERT": ["INSERT"], "PUMP": ["PUMP"], "FILTER": ["FILTER"], "DRIVE": ["DRIVE"],
    "FUSE": ["FUSE"], "SEAL": ["SEAL"], "OIL": ["OIL"], "BEARING": ["BEARING", "BRG"],
    "GREASE": ["GREASE"],
}
SUPPLIERS = [
    ("A", "Deccan Bearings & Tools", "CSV", "INR"),
    ("B", "Global Industrial Supply", "JSON", "USD"),
    ("C", "Coromandel Drives & Spares", "CSV (other layout)", "INR"),
]
USD_INR = 83.0

# How each supplier writes each of our parts: (description, true part or None)
_A = [
    ("BRG 6205-2RS DEEP GROOVE", "BEARING-6205"), ("BRG 6204-2RS DEEP GROOVE", None),
    ("BRG 6206-2RS DEEP GROOVE", None),
    ("INSERT CNMG-120408 CARBIDE", "TOOL-INSERT-T12"), ("INSERT CNMG-120404 CARBIDE", None),
    ("OIL SEAL 45X62X8 NBR", "GEARBOX-SEAL-GS7"), ("OIL SEAL 45X62X10 NBR", None),
    ("GEAR OIL VG220 5LTR", "GEAR-OIL-GO5"), ("GEAR OIL VG320 5LTR", None),
    ("GREASE LITHIUM EP2 1KG", "BEARING-GREASE-BG1"), ("GREASE LITHIUM EP3 1KG", None),
    ("FILTER CART CF1-25 MICRON", "COOLANT-FILTER-CF1"),
]
_B = [
    ("Nordlager 6205-2RS1 Deep Groove Ball Bearing 25x52x15mm", "BEARING-6205"),
    ("Nordlager 6305-2RS1 Deep Groove Ball Bearing 25x62x17mm", None),
    ("Kalmar CNMG 12 04 08-PM Turning Insert", "TOOL-INSERT-T12"),
    ("Kalmar DNMG 12 04 08-PM Turning Insert", None),
    ("Coolant Pump CP4-370 0.37kW 3ph", "COOLANT-PUMP-CP4"), ("Coolant Pump CP2-370 0.37kW 3ph", None),
    ("Coolant Filter Cartridge CF1-25", "COOLANT-FILTER-CF1"), ("Coolant Filter Cartridge CF2-25", None),
    ("Spindle Servo Drive SD2 7.5 kW", "SPINDLE-DRIVE-SD2"), ("Spindle Servo Drive SD2 5.5 kW", None),
    ("Voltgard F30 690V Semiconductor Fuse", "DRIVE-FUSE-F30"), ("Voltgard F30 500V Semiconductor Fuse", None),
    ("Oil Seal 45 x 62 x 8 mm NBR", "GEARBOX-SEAL-GS7"),
    ("ISO VG 220 Industrial Gear Oil, 5 L", "GEAR-OIL-GO5"), ("ISO VG 150 Industrial Gear Oil, 5 L", None),
    ("Lithium EP2 Bearing Grease 1 kg", "BEARING-GREASE-BG1"),
]
_C = [
    ("Brg. 6205 ZZ", "BEARING-6205"), ("Brg. 6206 ZZ", None),
    ("Carbide Insert CNMG120480", "TOOL-INSERT-T12"),          # supplier typo: 480 for 408
    ("Cool. Pump CP-4 370W", "COOLANT-PUMP-CP4"), ("Cool. Pump CP-4 550W", None),
    ("Filter CF 1-25", "COOLANT-FILTER-CF1"),
    ("Drive SD2-7.5", "SPINDLE-DRIVE-SD2"), ("Drive SD3-7.5", None),
    ("Fuse F30/690", "DRIVE-FUSE-F30"), ("Fuse F32/690", None),
    ("Seal 45*62*8", "GEARBOX-SEAL-GS7"),
    ("Gear Oil 220 (5L)", "GEAR-OIL-GO5"),                     # no 'VG': nothing to match on
]
_NOISE = ["HEX BOLT M8X40", "CABLE TIE 200MM", "V-BELT A42", "SAFETY GLOVES L", "HOSE CLAMP 32MM",
          "PROXIMITY SENSOR M12", "CONTACTOR 25A", "LIMIT SWITCH", "O-RING 20X2", "SPRAY PAINT GREY"]


def _price(rng, part: str | None, factor: float) -> float:
    base = {p[0]: p[4] for p in PART_MASTER}.get(part, float(rng.integers(80, 3000)))
    return float(base) * factor * float(rng.uniform(0.9, 1.15))


def generate_suppliers(seed: int = 42) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed + 500)
    audit, a_rows, b_rows, c_rows = [], [], [], []
    noise = lambda k: [(n, None) for n in rng.choice(_NOISE, size=k, replace=False)]  # noqa: E731

    for i, (desc, part) in enumerate(_A + noise(6)):
        code = f"DBT-{40100 + 7 * i}"
        a_rows.append(dict(item_code=code, item_desc=desc, price_inr=round(_price(rng, part, 1.0), 2),
                           lead_days=int(rng.integers(3, 8)), moq=int(rng.choice([1, 2, 5, 10]))))
        audit.append(dict(supplier_id="A", supplier_code=code, true_part_no=part))
    for i, (desc, part) in enumerate(_B + noise(6)):
        code = f"GIS-{880000 + 37 * i}"
        rec = {"sku": code, "title": desc,
               "price": {"usd": round(_price(rng, part, 0.92) / USD_INR, 2), "per": "each"},
               "lead_time_weeks": int(rng.integers(2, 5)), "pack_qty": int(rng.choice([1, 1, 5, 10]))}
        b_rows.append(dict(raw_json=json.dumps(rec)))
        audit.append(dict(supplier_id="B", supplier_code=code, true_part_no=part))
    for i, (desc, part) in enumerate(_C + noise(6)):
        code = f"CDS/{2300 + 11 * i}"
        rate = _price(rng, part, 1.05)
        days = int(rng.integers(5, 16))
        delivery = f"{days} days" if rng.random() < 0.5 else f"{max(1, round(days / 7))} wks"
        c_rows.append(dict(partnumber=code, name=desc, rate=f"Rs. {rate:,.2f}", delivery=delivery))
        audit.append(dict(supplier_id="C", supplier_code=code, true_part_no=part))

    return {
        "PART_MASTER": pd.DataFrame([p[:4] for p in PART_MASTER],
                                    columns=["part_no", "description", "category", "match_key"]),
        "CATEGORY_TERMS": pd.DataFrame([(c, t) for c, ts in CATEGORY_TERMS.items() for t in ts],
                                       columns=["category", "term"]),
        "SUPPLIERS": pd.DataFrame(SUPPLIERS, columns=["supplier_id", "name", "catalog_format", "currency"]),
        "FX_RATES": pd.DataFrame([("USD", USD_INR), ("INR", 1.0)], columns=["currency", "inr_per_unit"]),
        "SRC_SUPPLIER_A": pd.DataFrame(a_rows),
        "SRC_SUPPLIER_B": pd.DataFrame(b_rows),
        "SRC_SUPPLIER_C": pd.DataFrame(c_rows),
        "SUPPLIER_MATCH_AUDIT": pd.DataFrame(audit),
    }
