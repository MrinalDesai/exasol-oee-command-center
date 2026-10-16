"""Synthetic plant data generator.

Produces a reproducible fleet of machines across several plants with:
  - MACHINES        static asset register
  - TELEMETRY       hourly sensor readings per machine
  - DOWNTIME        unplanned stops (failures) and planned maintenance stops
  - SHIFT_PRODUCTION  per-shift output counts for OEE
  - MAINT_NOTES     free-text technician notes attached to each stop
  - SPARE_PARTS     on-hand stock of parts by store (plants + a warehouse)
  - LOCATIONS, TRANSFER_LANES, REPAIR_KITS  parts network and what each repair consumes

Failure physics: the failure modes follow those described for the public
AI4I 2020 predictive-maintenance dataset (tool wear, heat dissipation,
power, overstrain), plus a bearing-vibration mode. Tool wear fails on
accumulated wear. The other four develop along a P-F curve: a fault starts
at random, degrades over 18-48 hours and leaves its own footprint on the
sensor streams before the machine stops:
  HEAT_DISSIPATION  coolant fails, the process-air temperature gap collapses
  POWER             drive fault, power draw drifts out of band and swings
  OVERSTRAIN        feed jam / overload, torque climbs above nominal
  BEARING           bearing defect, vibration ramps up
No real or proprietary data is used.

Usage:
    python -m oee.generate --out data/generated --days 60 --seed 42
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

PLANTS = {
    "PNQ": "Pune",
    "BLR": "Bengaluru",
    "CHN": "Chennai",
}
MACHINE_TYPES = {
    # type: (ideal_cycle_seconds, nominal_rpm, nominal_torque_nm)
    "CNC_MILL": (42.0, 1500.0, 40.0),
    "CNC_LATHE": (35.0, 1800.0, 35.0),
    "PRESS": (12.0, 600.0, 65.0),
    "PACKER": (6.0, 900.0, 15.0),
}
FAILURE_MODES = ["TOOL_WEAR", "HEAT_DISSIPATION", "POWER", "OVERSTRAIN", "BEARING"]
FAILURE_PARTS = {
    "TOOL_WEAR": "TOOL-INSERT-T12",
    "HEAT_DISSIPATION": "COOLANT-PUMP-CP4",
    "POWER": "SPINDLE-DRIVE-SD2",
    "OVERSTRAIN": "GEARBOX-SEAL-GS7",
    "BEARING": "BEARING-6205",
}
REPAIR_HOURS = {  # (mean, sd) of time to repair
    "TOOL_WEAR": (1.5, 0.5),
    "HEAT_DISSIPATION": (3.0, 1.0),
    "POWER": (5.0, 1.5),
    "OVERSTRAIN": (4.0, 1.2),
    "BEARING": (6.0, 2.0),
}
NOTE_TEMPLATES = {
    "TOOL_WEAR": [
        "Insert chipped on {m}. Replaced {p}. Surface finish was degrading for last few batches.",
        "Tool wear limit exceeded on {m}; operator reported chatter marks. Swapped {p}.",
        "Cutting edge worn out, dimensional drift on parts from {m}. Fitted new {p}.",
    ],
    "HEAT_DISSIPATION": [
        "Process temp too close to ambient on {m}, coolant flow weak. Replaced {p}.",
        "Overheat trip on {m}. Coolant line partially blocked, flushed and changed {p}.",
        "Heat not dissipating, spindle housing hot to touch on {m}. {p} impeller worn.",
    ],
    "POWER": [
        "Drive fault alarm on {m}, power draw outside window. Replaced {p}.",
        "Spindle stalled under load on {m}. Drive module {p} failed, swapped from stores.",
        "Intermittent power loss on {m}; traced to {p}. Replaced and re-tuned.",
    ],
    "OVERSTRAIN": [
        "Overload on {m} after running high torque with worn tooling. Gearbox seal {p} leaking.",
        "Torque spike tripped {m}. Found oil leak, replaced {p}.",
        "Overstrain stop on {m}, gearbox noisy. {p} replaced, oil topped up.",
    ],
    "BEARING": [
        "High vibration on {m}, bearing noise audible. Replaced {p}.",
        "Bearing seized on {m} spindle. Fitted new {p}, checked alignment.",
        "Vibration trending up for days on {m}; bearing {p} spalled on inspection.",
    ],
    "PLANNED": [
        "Scheduled PM on {m}: lubrication, filter change, inspection. No defects found.",
        "Planned maintenance on {m}. Replaced consumables, calibrated sensors.",
        "Preventive maintenance completed on {m}. Tool changed as per schedule.",
    ],
}


@dataclass
class Machine:
    machine_id: str
    plant_id: str
    line_id: str
    machine_type: str
    install_year: int
    ideal_cycle_s: float
    nominal_rpm: float
    nominal_torque: float
    frailty: float  # per-machine multiplier on failure hazard


def _make_machines(rng: np.random.Generator, per_plant: int) -> list[Machine]:
    machines = []
    types = list(MACHINE_TYPES)
    for plant in PLANTS:
        for i in range(per_plant):
            mtype = types[i % len(types)]
            cyc, rpm, torque = MACHINE_TYPES[mtype]
            machines.append(
                Machine(
                    machine_id=f"{plant}-{mtype.split('_')[-1]}-{i + 1:02d}",
                    plant_id=plant,
                    line_id=f"{plant}-L{(i // 4) + 1}",
                    machine_type=mtype,
                    install_year=int(rng.integers(2012, 2024)),
                    ideal_cycle_s=cyc,
                    nominal_rpm=rpm,
                    nominal_torque=torque,
                    frailty=float(rng.lognormal(0.0, 0.35)),
                )
            )
    return machines


# P-F degradation: (start rate per machine-hour, min hours, max hours)
DEGRADATION = {
    "HEAT_DISSIPATION": (0.0011, 18, 40),
    "POWER": (0.0008, 18, 40),
    "OVERSTRAIN": (0.0010, 18, 36),
    "BEARING": (0.0010, 24, 48),
}
RANDOM_HAZARD = 0.00004   # failures with no warning at all (any mode), per hour


def _wear_hazard(tool_wear: float) -> float:
    """Per-hour hazard of a tool-wear failure."""
    return 0.0006 * np.exp(max(tool_wear - 170, 0) / 22.0)


def _fault_hazard(progress: float) -> float:
    """Per-hour hazard of a developing fault failing; progress runs 0 -> 1 over the P-F interval."""
    return 0.002 * np.exp(5.5 * progress)


def generate(days: int = 60, seed: int = 42, per_plant: int = 8, start: str = "2026-07-01",
             with_production: bool = True):
    rng = np.random.default_rng(seed)
    machines = _make_machines(rng, per_plant)
    hours = pd.date_range(start=start, periods=days * 24, freq="h")

    tel_rows, stop_rows, note_rows = [], [], []
    stop_id = 0

    for m in machines:
        tool_wear = float(rng.uniform(0, 120))
        vib_base = 1.8 if m.machine_type in ("CNC_MILL", "CNC_LATHE") else 2.4
        vib_drift = 0.0
        fault = None              # (mode, started, P-F hours, direction) of a developing fault
        down_until = None
        next_pm = hours[0] + pd.Timedelta(days=int(rng.integers(5, 15)))

        for ts in hours:
            # Machine is down: record nothing (no telemetry while stopped)
            if down_until is not None and ts < down_until:
                continue
            down_until = None

            # Planned maintenance resets wear + vibration drift
            if ts >= next_pm:
                dur = float(np.clip(rng.normal(3.0, 0.5), 2.0, 4.5))
                stop_id += 1
                stop_rows.append(
                    dict(
                        stop_id=stop_id, machine_id=m.machine_id, start_ts=ts,
                        end_ts=ts + pd.Timedelta(hours=dur), stop_type="PLANNED",
                        failure_mode=None, part_replaced=None,
                    )
                )
                note_rows.append(
                    dict(
                        stop_id=stop_id,
                        note_text=rng.choice(NOTE_TEMPLATES["PLANNED"]).format(m=m.machine_id, p=""),
                    )
                )
                tool_wear, vib_drift = 0.0, 0.0
                if fault is not None and rng.random() < 0.6:
                    fault = None          # planned maintenance finds most developing faults
                down_until = ts + pd.Timedelta(hours=int(np.ceil(dur)))
                next_pm = ts + pd.Timedelta(days=int(rng.integers(10, 18)))
                continue

            # Start a developing fault (at most one at a time)
            if fault is None:
                for fmode, (rate, lo, hi) in DEGRADATION.items():
                    if rng.random() < rate * m.frailty:
                        fault = (fmode, ts, float(rng.uniform(lo, hi)), float(rng.choice([-1.0, 1.0])))
                        break
            progress = 0.0
            if fault is not None:
                progress = (ts - fault[1]).total_seconds() / 3600.0 / fault[2]
            fmode = fault[0] if fault is not None else None

            # Sensor model
            air_temp = 298.0 + 2.0 * np.sin(2 * np.pi * (ts.dayofyear / 365.0)) + 1.5 * np.sin(
                2 * np.pi * ts.hour / 24.0) + rng.normal(0, 0.5)
            coolant_eff = 1.0 - 0.05 * (tool_wear / 250.0) + rng.normal(0, 0.05)
            if fmode == "HEAT_DISSIPATION":
                coolant_eff -= 0.45 * progress
            process_temp = air_temp + 10.0 * coolant_eff + rng.normal(0, 0.4)
            rpm = m.nominal_rpm * (1 + rng.normal(0, 0.06))
            torque = m.nominal_torque * (1 + rng.normal(0, 0.08)) * (1 + 0.05 * tool_wear / 250.0)
            if fmode == "OVERSTRAIN":
                torque *= 1 + 0.45 * progress
            power_kw = torque * rpm * 2 * np.pi / 60.0 / 1000.0
            if m.machine_type == "PRESS":
                power_kw *= 1.6
            elif m.machine_type == "PACKER":
                power_kw *= 4.0
            if fmode == "POWER":
                power_kw *= 1 + fault[3] * 0.35 * progress + rng.normal(0, 0.12 * progress)
            vib_drift += max(rng.normal(0.002, 0.005), 0) * m.frailty
            vibration = vib_base + vib_drift + abs(rng.normal(0, 0.15))
            if fmode == "BEARING":
                vibration += 2.2 * progress ** 1.5

            tel_rows.append(
                dict(
                    machine_id=m.machine_id, reading_ts=ts, air_temp_k=round(air_temp, 2),
                    process_temp_k=round(process_temp, 2), rpm=round(rpm, 1),
                    torque_nm=round(torque, 2), tool_wear_min=round(tool_wear, 1),
                    vibration_mm_s=round(vibration, 3), power_kw=round(power_kw, 3),
                )
            )

            # Failure draw (competing risks)
            hz = {k: RANDOM_HAZARD / len(FAILURE_MODES) for k in FAILURE_MODES}
            hz["TOOL_WEAR"] += _wear_hazard(tool_wear)
            if fmode is not None:
                hz[fmode] += _fault_hazard(progress) if progress < 1.25 else 1.0
            total = min(sum(hz.values()), 1.0)
            if rng.random() < total:
                modes = list(hz)
                probs = np.array([hz[k] for k in modes]) / sum(hz.values())
                mode = str(rng.choice(modes, p=probs))
                mu, sd = REPAIR_HOURS[mode]
                dur = float(np.clip(rng.normal(mu, sd), 0.5, mu * 3))
                stop_id += 1
                part = FAILURE_PARTS[mode]
                stop_rows.append(
                    dict(
                        stop_id=stop_id, machine_id=m.machine_id, start_ts=ts + pd.Timedelta(minutes=30),
                        end_ts=ts + pd.Timedelta(minutes=30) + pd.Timedelta(hours=dur),
                        stop_type="UNPLANNED", failure_mode=mode, part_replaced=part,
                    )
                )
                note_rows.append(
                    dict(stop_id=stop_id,
                         note_text=rng.choice(NOTE_TEMPLATES[mode]).format(m=m.machine_id, p=part))
                )
                if mode in ("TOOL_WEAR", "OVERSTRAIN"):
                    tool_wear = 0.0
                if mode == "BEARING":
                    vib_drift = 0.0
                if mode == fmode or mode != "TOOL_WEAR":
                    fault = None          # the repair clears the developing fault
                down_until = ts + pd.Timedelta(hours=int(np.ceil(dur + 0.5)))
                continue

            tool_wear += float(rng.uniform(0.8, 1.6)) * 60.0 / 60.0 * 1.0 * (1.0 if m.machine_type != "PACKER" else 0.5)

    machines_df = pd.DataFrame([m.__dict__ for m in machines]).drop(columns=["frailty"])
    telemetry = pd.DataFrame(tel_rows)
    downtime = pd.DataFrame(stop_rows)
    notes = pd.DataFrame(note_rows)
    production = _shift_production(rng, machines, hours, downtime, telemetry) if with_production else pd.DataFrame()
    parts = _spare_parts(rng)
    return dict(
        MACHINES=machines_df,
        TELEMETRY=telemetry,
        DOWNTIME=downtime,
        MAINT_NOTES=notes,
        SHIFT_PRODUCTION=production,
        SPARE_PARTS=parts,
        LOCATIONS=pd.DataFrame(LOCATIONS, columns=["location_id", "name", "kind"]),
        TRANSFER_LANES=_transfer_lanes(),
        REPAIR_KITS=pd.DataFrame(
            [(mode, part, qty) for mode, kit in REPAIR_KITS.items() for part, qty in kit],
            columns=["failure_mode", "part_no", "qty"]),
    )


# Planted operational problems, so the OEE breakdown has something to find.
# Line-level speed losses (performance) and a machine whose scrap rate climbs
# over the period (quality, e.g. a wearing die on a press).
SLOW_LINES = {"CHN-L2": 0.78}            # mean speed efficiency (fleet default 0.90)
QUALITY_DRIFT = {"CHN-PRESS-03": (0.025, 0.11)}  # scrap rate from start -> end of period
NIGHT_SHIFT_PENALTY = 0.03               # small performance dip on the 00:00-08:00 shift


def _scrap_from_condition(tool_wear: float, vib_excess: float, temp_diff: float) -> float:
    """Extra scrap rate caused by machine condition during the shift: a worn tool cuts
    out of tolerance, vibration leaves chatter marks, poor cooling distorts parts."""
    return (0.05 / (1.0 + np.exp(-(tool_wear - 190.0) / 15.0))
            + 0.04 * max(vib_excess - 0.6, 0.0)
            + 0.015 * max(8.0 - temp_diff, 0.0))


def _shift_production(rng, machines, hours, downtime: pd.DataFrame, telemetry: pd.DataFrame | None = None) -> pd.DataFrame:
    """Three 8h shifts/day. Run time = planned time minus downtime overlapping the shift.
    Scrap depends on the machine's condition during the shift (see _scrap_from_condition)."""
    rows = []
    cond = {}
    if telemetry is not None and len(telemetry):
        t = telemetry.assign(
            shift_start=telemetry.reading_ts.dt.floor("8h"),
            temp_diff=telemetry.process_temp_k - telemetry.air_temp_k)
        g = t.groupby(["machine_id", "shift_start"]).agg(
            wear=("tool_wear_min", "mean"), vib=("vibration_mm_s", "mean"), td=("temp_diff", "mean"))
        cond = {k: tuple(v) for k, v in zip(g.index, g.itertuples(index=False))}
    shift_starts = pd.date_range(hours[0], hours[-1], freq="8h")
    n_shifts = len(shift_starts)
    dt_by_m = {k: g for k, g in downtime.groupby("machine_id")} if len(downtime) else {}
    for m in machines:
        stops = dt_by_m.get(m.machine_id)
        speed_mean = SLOW_LINES.get(m.line_id, 0.90)
        drift = QUALITY_DRIFT.get(m.machine_id)
        for k, s in enumerate(shift_starts):
            e = s + pd.Timedelta(hours=8)
            planned_min = 8 * 60 - 30  # 30 min break
            lost = 0.0
            if stops is not None:
                ov = stops[(stops.start_ts < e) & (stops.end_ts > s)]
                for _, r in ov.iterrows():
                    lost += (min(r.end_ts, e) - max(r.start_ts, s)).total_seconds() / 60.0
            run_min = max(planned_min - lost, 0.0)
            mean = speed_mean - (NIGHT_SHIFT_PENALTY if s.hour == 0 else 0.0)
            speed_eff = float(np.clip(rng.normal(mean, 0.05), 0.5, 1.0))
            total = int(run_min * 60.0 / m.ideal_cycle_s * speed_eff)
            if drift:
                scrap_mean = drift[0] + (drift[1] - drift[0]) * k / max(n_shifts - 1, 1)
            else:
                scrap_mean = 0.025
            c = cond.get((m.machine_id, s))
            if c is not None:
                vib_base = 1.8 if m.machine_type in ("CNC_MILL", "CNC_LATHE") else 2.4
                scrap_mean += _scrap_from_condition(c[0], c[1] - vib_base, c[2])
            scrap_rate = float(np.clip(rng.normal(scrap_mean, 0.012), 0.0, 0.2))
            good = int(total * (1 - scrap_rate))
            rows.append(
                dict(
                    machine_id=m.machine_id, shift_start=s, shift_end=e,
                    planned_minutes=float(planned_min), downtime_minutes=round(lost, 1),
                    ideal_cycle_s=m.ideal_cycle_s, total_count=total, good_count=good,
                )
            )
    return pd.DataFrame(rows)


# Stores that hold spare parts: the three plants and a regional warehouse
LOCATIONS = [
    ("PNQ", "Pune plant store", "PLANT"),
    ("BLR", "Bengaluru plant store", "PLANT"),
    ("CHN", "Chennai plant store", "PLANT"),
    ("HYD", "Hyderabad regional warehouse", "WAREHOUSE"),
]
# Road transfer time between stores, hours (symmetric)
LANE_HOURS = {("PNQ", "BLR"): 20, ("BLR", "CHN"): 8, ("PNQ", "CHN"): 28,
              ("HYD", "PNQ"): 12, ("HYD", "BLR"): 10, ("HYD", "CHN"): 12}
# What a repair of each failure mode consumes
REPAIR_KITS = {
    "TOOL_WEAR": [("TOOL-INSERT-T12", 4)],
    "HEAT_DISSIPATION": [("COOLANT-PUMP-CP4", 1), ("COOLANT-FILTER-CF1", 1)],
    "POWER": [("SPINDLE-DRIVE-SD2", 1), ("DRIVE-FUSE-F30", 2)],
    "OVERSTRAIN": [("GEARBOX-SEAL-GS7", 1), ("GEAR-OIL-GO5", 1)],
    "BEARING": [("BEARING-6205", 2), ("BEARING-GREASE-BG1", 1)],
}
from oee.suppliers import PART_MASTER as _PART_MASTER  # noqa: E402  (list prices live with the part master)

PART_PRICE_INR = {p[0]: p[4] for p in _PART_MASTER}
# Stocking policy: where each part is held in bulk (its home store)
HOME_STORE = {
    "TOOL-INSERT-T12": "BLR",       # tooling hub
    "COOLANT-PUMP-CP4": "HYD",      # expensive, held centrally
    "COOLANT-FILTER-CF1": "PNQ",
    "SPINDLE-DRIVE-SD2": "HYD",
    "DRIVE-FUSE-F30": "CHN",
    "GEARBOX-SEAL-GS7": "HYD",
    "GEAR-OIL-GO5": "PNQ",
    "BEARING-6205": "CHN",
    "BEARING-GREASE-BG1": "HYD",
}
CONSUMABLES = {"COOLANT-FILTER-CF1", "DRIVE-FUSE-F30", "GEAR-OIL-GO5", "BEARING-GREASE-BG1"}


def _transfer_lanes() -> pd.DataFrame:
    rows = []
    for (a, b), h in LANE_HOURS.items():
        for x, y in ((a, b), (b, a)):
            rows.append(dict(from_location=x, to_location=y, transfer_hours=h, cost_inr=1500 + 90 * h))
    return pd.DataFrame(rows)


def _spare_parts(rng) -> pd.DataFrame:
    """Stock per store. Each part is held in bulk at its home store (HOME_STORE; the
    expensive ones at the regional warehouse); other stores hold a few or none. So
    the store next to a failing machine is usually missing part of the kit even
    though the network as a whole has it: parts in the wrong location."""
    rows = []
    stores = [loc for loc, _, _ in LOCATIONS]
    parts = sorted({p for kit in REPAIR_KITS.values() for p, _ in kit})
    for part in parts:
        consumable = part in CONSUMABLES
        home = HOME_STORE[part]
        lead = int(rng.integers(2, 6)) if consumable else int(rng.integers(7, 29))
        cost = int(round(PART_PRICE_INR[part] * float(rng.uniform(0.95, 1.1))))
        empty_share = 0.55 if consumable else 0.7
        for store in stores:
            if store == home:
                on_hand = int(rng.integers(5, 13))
            else:
                on_hand = 0 if rng.random() < empty_share else int(rng.integers(1, 3))
            rows.append(dict(plant_id=store, part_no=part, on_hand=on_hand,
                             reorder_point=2 if store != "HYD" else 4,
                             lead_time_days=lead, unit_cost_inr=cost))
    return pd.DataFrame(rows)


def generate_plant(days: int = 60, seed: int = 42, per_plant: int = 8) -> dict:
    """The demo plant as Exasol receives it: production, downtime and parts as before,
    but sensor data scattered across five source systems (see oee/sources.py).

    TELEMETRY_TRUTH (the clean hourly physics) is returned for tests and is not
    loaded into Exasol: the database rebuilds telemetry from the sources."""
    from oee.scale import _upsample
    from oee.sources import scatter

    tables = generate(days=days, seed=seed, per_plant=per_plant)
    truth_hourly = tables.pop("TELEMETRY")
    minute = _upsample(truth_hourly, 1, np.random.default_rng(seed + 100))
    as_of = minute["reading_ts"].max() + pd.Timedelta(minutes=1)
    tables.update(scatter(minute, tables["MACHINES"], as_of=as_of, seed=seed + 200))
    tables["FUSION_CONFIG"] = pd.DataFrame({"DATA_AS_OF": [as_of]})
    from oee.suppliers import generate_suppliers

    tables.update(generate_suppliers(seed))
    from oee.stores import split_stores

    tables.update(split_stores(tables.pop("SPARE_PARTS")))   # stock lives in four store systems
    tables["TELEMETRY_TRUTH"] = truth_hourly
    return tables


def write_tables(tables: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        if name == "TELEMETRY_TRUTH":
            continue
        df.to_csv(out / f"{name.lower()}.csv", index=False, date_format="%Y-%m-%d %H:%M:%S", lineterminator="\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/generated")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--per-plant", type=int, default=8)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tables = generate(days=args.days, seed=args.seed, per_plant=args.per_plant)
    for name, df in tables.items():
        df.to_csv(out / f"{name.lower()}.csv", index=False)
        print(f"{name:18s} {len(df):>8,d} rows -> {out / (name.lower() + '.csv')}")


if __name__ == "__main__":
    main()
