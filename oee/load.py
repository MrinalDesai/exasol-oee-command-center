"""Load generated CSVs into the OEE schema using pyexasol's parallel HTTP import."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

TABLES = {
    # table: (csv file, timestamp columns)
    "MACHINES": ("machines.csv", []),
    "TELEMETRY": ("telemetry.csv", ["reading_ts"]),
    "DOWNTIME": ("downtime.csv", ["start_ts", "end_ts"]),
    "MAINT_NOTES": ("maint_notes.csv", []),
    "SHIFT_PRODUCTION": ("shift_production.csv", ["shift_start", "shift_end"]),
    "SPARE_PARTS": ("spare_parts.csv", []),
}


def load_all(conn, data_dir: str | Path) -> dict[str, int]:
    data_dir = Path(data_dir)
    counts = {}
    for table, (fname, ts_cols) in TABLES.items():
        df = pd.read_csv(data_dir / fname, parse_dates=ts_cols)
        df.columns = [c.upper() for c in df.columns]
        for c in ts_cols:
            df[c.upper()] = df[c.upper()].dt.strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(f"TRUNCATE TABLE OEE.{table}")
        conn.import_from_pandas(df, ("OEE", table))
        n = conn.execute(f"SELECT COUNT(*) FROM OEE.{table}").fetchval()
        if n != len(df):
            raise RuntimeError(f"{table}: loaded {n} rows, expected {len(df)}")
        counts[table] = n
        print(f"  loaded {table:18s} {n:>8,d} rows")
    return counts
