"""Shape the Exasol diagnosis views into the JSON the console renders.

All of the analysis lives in sql/06_root_cause.sql and sql/07_diagnosis.sql:
  V_MACHINE_STATUS      one row per machine: health, likely mode, part, stock,
                        open work order, pipeline stage, expected downtime
  V_MODE_LIKELIHOOD     root-cause probability of each failure mode per machine
  V_SIGNAL_DEVIATIONS   each signal vs its machine type's normal, ranked

This module only groups and renames those rows; it computes nothing.
"""
from __future__ import annotations

MODES = ["TOOL_WEAR", "HEAT_DISSIPATION", "POWER", "OVERSTRAIN", "BEARING"]

STATUS_SQL = """
SELECT MACHINE_ID, PLANT_ID, LINE_ID, MACHINE_TYPE, READING_TS, RISK_24H, MTBF_HOURS, MTTR_HOURS, FAILURES,
       HEALTH, TOP_MODE, TOP_MODE_P, PART_NO, ON_HAND, LEAD_TIME_DAYS, UNPLANNED_HOURS, PLANNED_HOURS,
       OPEN_WO_ID, OPEN_WO_MODE, STAGE, EXPECTED_DOWN_HOURS_24H
FROM OEE.V_MACHINE_STATUS
{where}
ORDER BY LINE_ID, MACHINE_ID"""

MODES_SQL = """
SELECT MACHINE_ID, FAILURE_MODE, PART_NO, PROBABILITY, HISTORY_STOPS, SIGNATURE_DISTANCE, MODE_RANK
FROM OEE.V_MODE_LIKELIHOOD
{where}
ORDER BY MACHINE_ID, MODE_RANK"""

SIGNALS_SQL = """
SELECT MACHINE_ID, SIGNAL_NAME, SIGNAL_VALUE, BASELINE, Z, DEV_RANK
FROM OEE.V_SIGNAL_DEVIATIONS
{where}
ORDER BY MACHINE_ID, DEV_RANK"""

# Display names only (presentation)
SIGNAL_LABELS = {
    "TOOL_WEAR_MIN": ("Tool wear", "min"),
    "VIBRATION_MM_S": ("Vibration", "mm/s"),
    "TEMP_DIFF_K": ("Process minus air temperature", "K"),
    "POWER_KW": ("Power", "kW"),
    "TORQUE_RATIO": ("Torque vs nominal", "×"),
}


def queries(machine: str | None = None) -> list[str]:
    where = f"WHERE MACHINE_ID = '{machine}'" if machine else ""
    return [q.format(where=where) for q in (STATUS_SQL, MODES_SQL, SIGNALS_SQL)]


def shape(status_rows: list[dict], mode_rows: list[dict], signal_rows: list[dict]) -> list[dict]:
    modes: dict[str, list[dict]] = {}
    for r in mode_rows:
        modes.setdefault(r["MACHINE_ID"], []).append(
            {"mode": r["FAILURE_MODE"], "p": r["PROBABILITY"], "history": int(r["HISTORY_STOPS"]),
             "part_no": r["PART_NO"], "distance": r["SIGNATURE_DISTANCE"]})
    sigs: dict[str, list[dict]] = {}
    for r in signal_rows:
        label, unit = SIGNAL_LABELS.get(r["SIGNAL_NAME"], (r["SIGNAL_NAME"], ""))
        sigs.setdefault(r["MACHINE_ID"], []).append(
            {"signal": r["SIGNAL_NAME"], "label": label, "unit": unit, "value": r["SIGNAL_VALUE"],
             "baseline": r["BASELINE"], "z": r["Z"], "rank": int(r["DEV_RANK"])})

    out = []
    for r in status_rows:
        mid = r["MACHINE_ID"]
        s = sorted(sigs.get(mid, []), key=lambda d: d["rank"])
        wo = None
        if r.get("OPEN_WO_ID") is not None:
            wo = {"WO_ID": r["OPEN_WO_ID"], "FAILURE_MODE": r["OPEN_WO_MODE"]}
        out.append({
            "machine_id": mid,
            "plant_id": r["PLANT_ID"],
            "line_id": r["LINE_ID"],
            "machine_type": r["MACHINE_TYPE"],
            "reading_ts": r["READING_TS"],
            "risk": r["RISK_24H"],
            "status": r["HEALTH"],
            "mtbf_h": r.get("MTBF_HOURS"),
            "mttr_h": r.get("MTTR_HOURS"),
            "failures": r.get("FAILURES"),
            "likely_modes": modes.get(mid, []),
            "top_factors": s[:3],
            "signals": {d["signal"]: {"value": d["value"], "baseline": d["baseline"], "z": d["z"]} for d in s},
            "part": {"part_no": r["PART_NO"], "on_hand": r["ON_HAND"], "lead_time_days": r["LEAD_TIME_DAYS"]},
            "unplanned_hours": r["UNPLANNED_HOURS"],
            "planned_hours": r["PLANNED_HOURS"],
            "expected_down_hours": r["EXPECTED_DOWN_HOURS_24H"],
            "work_order": wo,
            "stage": r["STAGE"],
        })
    return out
