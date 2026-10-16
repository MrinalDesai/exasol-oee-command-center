"""Scatter the plant's sensor data across five source systems, the way it
arrives on a real shop floor.

The generator's physics give one clean, one-minute record per machine (the
"truth"). This module splits it into what five different systems would
actually deliver, each with its own identifiers, cadence, clock, units, gaps
and delays:

  SRC_CM_VIBRATION   condition monitor     sensor IDs, 1 min, UTC, dropouts and outages
  SRC_PLC_TEMPS      PLC historian         PLC tags, every 5 min, local time (IST), deg C
  SRC_DRIVE          drive controller      drive IDs, 1 min, UTC, gaps
  SRC_ENERGY         energy meter          meter IDs, 15-min kWh totals, arrive hours late
  SRC_MES_TOOL       production system     asset tags, irregular tool-wear events, local time

ASSET_REGISTRY ties the identifiers together. Exasol combines and completes
the streams in sql/02_fusion.sql. Nothing here is real plant data.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

UTC_OFFSET_MIN = 330          # India Standard Time, used by the PLC historian and the production system
ENERGY_INTERVAL_MIN = 15


def asset_registry(machines: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for k, m in enumerate(machines.itertuples(index=False)):
        pos = int(m.machine_id.split("-")[-1])
        rows.append(dict(
            machine_id=m.machine_id,
            asset_tag=f"A-{m.plant_id}-{1000 + 7 * k + 3}",
            cm_sensor_id=f"CM{4100 + 13 * k:05d}",
            plc_tag=f"PLC/{m.plant_id}/{m.line_id.split('-')[-1]}/{pos:02d}",
            drive_id=f"DRV-{m.plant_id[0]}{201 + k}",
            meter_id=f"EM.{m.plant_id}.{31 + k}",
            utc_offset_min=UTC_OFFSET_MIN,
        ))
    return pd.DataFrame(rows)


def _drop(df: pd.DataFrame, rng, rate: float, outages_per_day: float, outage_min: tuple[int, int],
          step_min: int = 1) -> pd.DataFrame:
    """Random single dropouts plus contiguous outages, per machine."""
    keep = rng.random(len(df)) >= rate
    if outages_per_day > 0:
        for mid, idx in df.groupby("machine_id").indices.items():
            ts = df["ts"].to_numpy()[idx]
            days = (ts[-1] - ts[0]) / np.timedelta64(1, "D") if len(ts) else 0
            for _ in range(rng.poisson(outages_per_day * days)):
                start = ts[0] + rng.random() * (ts[-1] - ts[0])
                end = start + np.timedelta64(int(rng.integers(*outage_min)), "m")
                keep[idx[(ts >= start) & (ts < end)]] = False
    return df[keep]


def scatter(truth: pd.DataFrame, machines: pd.DataFrame, as_of: pd.Timestamp, seed: int = 11) -> dict:
    """truth: one-minute readings (machine_id, reading_ts in UTC, sensor columns)."""
    rng = np.random.default_rng(seed)
    reg = asset_registry(machines)
    t = truth.merge(reg, on="machine_id").rename(columns={"reading_ts": "ts"})
    t = t.sort_values(["machine_id", "ts"]).reset_index(drop=True)
    local = pd.Timedelta(minutes=UTC_OFFSET_MIN)

    vib = _drop(t[["machine_id", "ts", "cm_sensor_id", "vibration_mm_s"]], rng, 0.02, 0.5, (20, 90))
    src_vib = pd.DataFrame({"SENSOR_ID": vib.cm_sensor_id, "TS_UTC": vib.ts,
                            "VIB_RMS": vib.vibration_mm_s.round(3)})

    five = t[t.ts.dt.minute % 5 == 0]
    five = _drop(five[["machine_id", "ts", "plc_tag", "air_temp_k", "process_temp_k"]], rng, 0.01, 0.2, (15, 60))
    src_plc = pd.DataFrame({"PLC_TAG": five.plc_tag, "TS_LOCAL": five.ts + local,
                            "AIR_C": (five.air_temp_k - 273.15).round(2),
                            "PROCESS_C": (five.process_temp_k - 273.15).round(2)})

    drv = _drop(t[["machine_id", "ts", "drive_id", "rpm", "torque_nm"]], rng, 0.03, 0.35, (10, 40))
    src_drive = pd.DataFrame({"DRIVE_ID": drv.drive_id, "TS_UTC": drv.ts, "RPM": drv.rpm, "TORQUE_NM": drv.torque_nm})

    e = t[["machine_id", "meter_id", "ts", "power_kw"]].copy()
    e["interval"] = e.ts.dt.floor(f"{ENERGY_INTERVAL_MIN}min")
    en = e.groupby(["machine_id", "meter_id", "interval"], as_index=False)["power_kw"].mean()
    en["kwh"] = (en.power_kw * ENERGY_INTERVAL_MIN / 60).round(4)
    delay_min = np.clip(rng.lognormal(np.log(120), 0.6, len(en)), 20, 600).astype(int)
    en["received"] = en.interval + pd.Timedelta(minutes=ENERGY_INTERVAL_MIN) + pd.to_timedelta(delay_min, unit="m")
    src_energy = pd.DataFrame({"METER_ID": en.meter_id, "INTERVAL_START_UTC": en.interval,
                               "KWH": en.kwh, "RECEIVED_AT_UTC": en.received})
    src_energy = src_energy[src_energy.RECEIVED_AT_UTC <= as_of]       # not arrived yet

    tool_rows = []
    for mid, g in t.groupby("machine_id", sort=False):
        n = len(g)
        pos, picks = 0, []
        while pos < n:
            picks.append(pos)
            pos += int(rng.integers(5, 26))           # the production system logs every 5-25 minutes
        tool_rows.append(g.iloc[picks][["asset_tag", "ts", "tool_wear_min"]])
    tool = pd.concat(tool_rows)
    src_tool = pd.DataFrame({"ASSET_TAG": tool.asset_tag, "EVENT_TS_LOCAL": tool.ts + local,
                             "TOOL_WEAR_MIN": tool.tool_wear_min})

    registry = reg.copy()
    registry.columns = [c.upper() for c in registry.columns]
    return {
        "ASSET_REGISTRY": registry,
        "SRC_CM_VIBRATION": src_vib.reset_index(drop=True),
        "SRC_PLC_TEMPS": src_plc.reset_index(drop=True),
        "SRC_DRIVE": src_drive.reset_index(drop=True),
        "SRC_ENERGY": src_energy.reset_index(drop=True),
        "SRC_MES_TOOL": src_tool.reset_index(drop=True),
    }
