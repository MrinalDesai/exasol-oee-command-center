"""Load the generated CSVs into the OEE schema with Exasol's IMPORT (streamed by pyexasol).

Sensor data arrives as five raw source tables (SRC_*); Exasol combines them in
sql/02_fusion.sql. Column order in each CSV matches the table definition in
sql/01_schema.sql.
"""
from __future__ import annotations

import time
from pathlib import Path

TABLES = [
    "MACHINES", "DOWNTIME", "MAINT_NOTES", "SHIFT_PRODUCTION",
    "SAP_PNQ.MARD", "SAP_BLR.MARD", "TALLY_CHN.STOCK_SUMMARY", "WMS_HYD.INVENTORY",
    "LOCATIONS", "TRANSFER_LANES", "REPAIR_KITS",
    "ASSET_REGISTRY", "SRC_CM_VIBRATION", "SRC_PLC_TEMPS", "SRC_DRIVE", "SRC_ENERGY", "SRC_MES_TOOL",
    "FUSION_CONFIG",
    "PART_MASTER", "CATEGORY_TERMS", "SUPPLIERS", "FX_RATES",
    "SRC_SUPPLIER_A", "SRC_SUPPLIER_B", "SRC_SUPPLIER_C", "SUPPLIER_MATCH_AUDIT",
]


def _csv_rows(path: Path) -> int:
    with open(path, "rb") as fh:
        return sum(1 for _ in fh) - 1          # minus header


def load_all(conn, data_dir: str | Path, log=print) -> dict[str, int]:
    from oee.db import connect

    data_dir = Path(data_dir)
    counts = {}
    with connect(compression=False) as loader:
        for table in TABLES:
            schema, name = table.split(".") if "." in table else ("OEE", table)
            path = data_dir / f"{table.lower()}.csv"
            expected = _csv_rows(path)
            for attempt in (1, 2, 3):
                conn.execute(f"TRUNCATE TABLE {schema}.{name}")
                t0 = time.perf_counter()
                try:
                    loader.import_from_file(str(path), (schema, name), import_params={"skip": 1})
                    break
                except Exception:                 # the HTTPS stream can drop at the very end; retry
                    if attempt == 3:
                        raise
            n = conn.execute(f"SELECT COUNT(*) FROM {schema}.{name}").fetchval()
            if n != expected:
                raise RuntimeError(f"{table}: loaded {n} rows, expected {expected}")
            counts[table] = n
            log(f"  loaded {table:18s} {n:>10,d} rows  {time.perf_counter() - t0:6.1f} s")
    return counts
