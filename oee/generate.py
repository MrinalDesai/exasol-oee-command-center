"""Synthetic plant data generator.

Produces a reproducible fleet of machines across several plants with:
  - MACHINES        static asset register
  - TELEMETRY       hourly sensor readings per machine
  - DOWNTIME        unplanned stops (failures) and planned maintenance stops
  - SHIFT_PRODUCTION  per-shift output counts for OEE
  - MAINT_NOTES     free-text technician notes attached to each stop
  - SPARE_PARTS     on-hand stock of parts by site

Failure physics loosely follow the failure modes described for the public
AI4I 2020 predictive-maintenance dataset (tool wear, heat dissipation,
power, overstrain), plus a bearing-vibration mode. No real or proprietary
data is used.

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


def _hazard(tool_wear, temp_diff, power_kw, strain, vib_excess, frailty):
    """Per-hour failure hazard for each mode (vectorised over nothing; scalars)."""
    return {
        "TOOL_WEAR": 0.0004 * np.exp(max(tool_wear - 180, 0) / 25.0),
        "HEAT_DISSIPATION": 0.0003 * np.exp(max(9.0 - temp_diff, 0) / 1.2),
        "POWER": 0.0002 * np.exp(abs(power_kw - 6.5) / 2.2),
        "OVERSTRAIN": 0.0002 * np.exp(max(strain - 10500, 0) / 900.0),
        "BEARING": 0.0002 * np.exp(max(vib_excess, 0) / 0.9),
    }, frailty


def generate(days: int = 60, seed: int = 42, per_plant: int = 8, start: str = "2026-07-01"):
    rng = np.random.default_rng(seed)
    machines = _make_machines(rng, per_plant)
    hours = pd.date_range(start=start, periods=days * 24, freq="h")

    tel_rows, stop_rows, note_rows = [], [], []
    stop_id = 0

    for m in machines:
        tool_wear = float(rng.uniform(0, 120))
        vib_base = 1.8 if m.machine_type in ("CNC_MILL", "CNC_LATHE") else 2.4
        vib_drift = 0.0
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
                down_until = ts + pd.Timedelta(hours=int(np.ceil(dur)))
                next_pm = ts + pd.Timedelta(days=int(rng.integers(10, 18)))
                continue

            # Sensor model
            air_temp = 298.0 + 2.0 * np.sin(2 * np.pi * (ts.dayofyear / 365.0)) + 1.5 * np.sin(
                2 * np.pi * ts.hour / 24.0) + rng.normal(0, 0.5)
            coolant_eff = 1.0 - 0.15 * (tool_wear / 250.0) + rng.normal(0, 0.08)
            process_temp = air_temp + 10.0 * coolant_eff + rng.normal(0, 0.4)
            rpm = m.nominal_rpm * (1 + rng.normal(0, 0.06))
            torque = m.nominal_torque * (1 + rng.normal(0, 0.10)) * (1 + 0.2 * tool_wear / 250.0)
            power_kw = torque * rpm * 2 * np.pi / 60.0 / 1000.0
            if m.machine_type == "PRESS":
                power_kw *= 1.6
            elif m.machine_type == "PACKER":
                power_kw *= 4.0
            vib_drift += max(rng.normal(0.004, 0.01), 0) * m.frailty
            vibration = vib_base + vib_drift + abs(rng.normal(0, 0.15))
            temp_diff = process_temp - air_temp
            strain = tool_wear * 40.0 * torque / m.nominal_torque  # normalised to a 40 Nm reference

            hz, frailty = _hazard(tool_wear, temp_diff, power_kw, strain, vibration - vib_base - 1.0, m.frailty)
            tel_rows.append(
                dict(
                    machine_id=m.machine_id, reading_ts=ts, air_temp_k=round(air_temp, 2),
                    process_temp_k=round(process_temp, 2), rpm=round(rpm, 1),
                    torque_nm=round(torque, 2), tool_wear_min=round(tool_wear, 1),
                    vibration_mm_s=round(vibration, 3), power_kw=round(power_kw, 3),
                )
            )

            # Failure draw (competing risks)
            total = sum(hz.values()) * frailty
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
                down_until = ts + pd.Timedelta(hours=int(np.ceil(dur + 0.5)))
                continue

            tool_wear += float(rng.uniform(0.8, 1.6)) * 60.0 / 60.0 * 1.0 * (1.0 if m.machine_type != "PACKER" else 0.5)

    machines_df = pd.DataFrame([m.__dict__ for m in machines]).drop(columns=["frailty"])
    telemetry = pd.DataFrame(tel_rows)
    downtime = pd.DataFrame(stop_rows)
    notes = pd.DataFrame(note_rows)
    production = _shift_production(rng, machines, hours, downtime)
    parts = _spare_parts(rng)
    return dict(
        MACHINES=machines_df,
        TELEMETRY=telemetry,
        DOWNTIME=downtime,
        MAINT_NOTES=notes,
        SHIFT_PRODUCTION=production,
        SPARE_PARTS=parts,
    )


# Planted operational problems, so the OEE breakdown has something to find.
# Line-level speed losses (performance) and a machine whose scrap rate climbs
# over the period (quality, e.g. a wearing die on a press).
SLOW_LINES = {"CHN-L2": 0.78}            # mean speed efficiency (fleet default 0.90)
QUALITY_DRIFT = {"CHN-PRESS-03": (0.025, 0.11)}  # scrap rate from start -> end of period
NIGHT_SHIFT_PENALTY = 0.03               # small performance dip on the 00:00-08:00 shift


def _shift_production(rng, machines, hours, downtime: pd.DataFrame) -> pd.DataFrame:
    """Three 8h shifts/day. Run time = planned time minus downtime overlapping the shift."""
    rows = []
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


def _spare_parts(rng) -> pd.DataFrame:
    rows = []
    for plant in PLANTS:
        for part in FAILURE_PARTS.values():
            rows.append(
                dict(
                    plant_id=plant, part_no=part, on_hand=int(rng.integers(0, 8)),
                    reorder_point=2, lead_time_days=int(rng.integers(2, 21)),
                    unit_cost_inr=int(rng.integers(800, 95000)),
                )
            )
    return pd.DataFrame(rows)


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
