"""Fleet assessment: turn Exasol query results into per-machine verdicts.

For each machine's latest reading this combines:
  - the 24-hour failure risk scored in-database by the FAILURE_RISK UDF,
  - how far each signal sits from its machine type's normal (z-scores computed in SQL),
  - the machine type's history of unplanned failure modes,
  - spare-parts stock at the machine's plant, and open work orders.

The likely failure mode is the machine type's historical mode mix, re-weighted
by the signals that are abnormal right now. It is a transparent rule, not a
second model, and the console says so.
"""
from __future__ import annotations

import math

MODES = ["TOOL_WEAR", "HEAT_DISSIPATION", "POWER", "OVERSTRAIN", "BEARING"]
MODE_PARTS = {
    "TOOL_WEAR": "TOOL-INSERT-T12",
    "HEAT_DISSIPATION": "COOLANT-PUMP-CP4",
    "POWER": "SPINDLE-DRIVE-SD2",
    "OVERSTRAIN": "GEARBOX-SEAL-GS7",
    "BEARING": "BEARING-6205",
}
# Signal z-score columns returned by FLEET_SQL, with the label shown in the console
SIGNALS = {
    "TW_Z": ("TOOL_WEAR_MIN", "TW_BASE", "Tool wear", "min"),
    "VIB_Z": ("VIBRATION_MM_S", "VIB_BASE", "Vibration", "mm/s"),
    "TD_Z": ("TEMP_DIFF_K", "TD_BASE", "Process minus air temperature", "K"),
    "PW_Z": ("POWER_KW", "PW_BASE", "Power", "kW"),
    "TR_Z": ("TORQUE_RATIO", "TR_BASE", "Torque vs nominal", "×"),
}
CRITICAL, WATCH = 0.5, 0.10  # watch: above the fleet's ~9% base failure rate


def status_for(risk: float | None) -> str:
    if risk is None:
        return "unknown"
    if risk >= CRITICAL:
        return "critical"
    if risk >= WATCH:
        return "watch"
    return "healthy"


def _evidence(row: dict) -> dict[str, float]:
    """How strongly the current signals point at each failure mode (in z units)."""
    z = {k: (row.get(k) or 0.0) for k in SIGNALS}
    return {
        "TOOL_WEAR": z["TW_Z"],
        "OVERSTRAIN": (z["TW_Z"] + z["TR_Z"]) / 2.0,
        "HEAT_DISSIPATION": -z["TD_Z"],          # heat builds when the temperature gap shrinks
        "POWER": abs(z["PW_Z"]),
        "BEARING": z["VIB_Z"],
    }


def likely_modes(row: dict, type_mix: dict[str, int]) -> list[dict]:
    """Historical mode mix for the machine type (Laplace-smoothed), re-weighted by signal evidence."""
    total = sum(type_mix.get(m, 0) for m in MODES) + len(MODES)
    ev = _evidence(row)
    scores = {}
    for m in MODES:
        prior = (type_mix.get(m, 0) + 1) / total
        scores[m] = prior * math.exp(0.8 * max(-2.0, min(3.0, ev[m])))
    s = sum(scores.values())
    out = [{"mode": m, "p": scores[m] / s, "history": type_mix.get(m, 0)} for m in MODES]
    return sorted(out, key=lambda d: d["p"], reverse=True)


def top_factors(row: dict, n: int = 3) -> list[dict]:
    out = []
    for zcol, (col, base, label, unit) in SIGNALS.items():
        z = row.get(zcol)
        if z is None:
            continue
        out.append({"signal": col, "label": label, "unit": unit, "value": row.get(col),
                    "baseline": row.get(base), "z": z})
    return sorted(out, key=lambda d: abs(d["z"]), reverse=True)[:n]


def assess(fleet_rows: list[dict], mode_rows: list[dict], planned_hours: float | None,
           parts_rows: list[dict], wo_rows: list[dict]) -> list[dict]:
    mix: dict[str, dict[str, int]] = {}
    hours_by_type: dict[tuple[str, str], float] = {}
    hours_fleet: dict[str, list[float]] = {}
    for r in mode_rows:
        mix.setdefault(r["MACHINE_TYPE"], {})[r["FAILURE_MODE"]] = int(r["STOPS"])
        hours_by_type[(r["MACHINE_TYPE"], r["FAILURE_MODE"])] = r["AVG_HOURS"]
        hours_fleet.setdefault(r["FAILURE_MODE"], []).append(r["AVG_HOURS"])
    stock = {(p["PLANT_ID"], p["PART_NO"]): p for p in parts_rows}
    open_wo = {w["MACHINE_ID"]: w for w in wo_rows}

    out = []
    for r in fleet_rows:
        modes = likely_modes(r, mix.get(r["MACHINE_TYPE"], {}))
        top = modes[0]["mode"]
        part_no = MODE_PARTS[top]
        p = stock.get((r["PLANT_ID"], part_no), {})
        unplanned_h = hours_by_type.get((r["MACHINE_TYPE"], top))
        if unplanned_h is None and hours_fleet.get(top):
            unplanned_h = sum(hours_fleet[top]) / len(hours_fleet[top])
        avoided = None
        if unplanned_h is not None and planned_hours is not None:
            avoided = max(unplanned_h - planned_hours, 0.0)
        wo = open_wo.get(r["MACHINE_ID"])
        on_hand = p.get("ON_HAND")
        stage = "DIAGNOSE"
        if wo:
            stage = "WORK_ORDER"
        elif on_hand is not None and on_hand <= 0:
            stage = "PARTS_BLOCKED"
        elif r.get("RISK_24H") is not None and r["RISK_24H"] >= WATCH:
            stage = "READY"
        out.append({
            "machine_id": r["MACHINE_ID"],
            "plant_id": r["PLANT_ID"],
            "line_id": r["LINE_ID"],
            "machine_type": r["MACHINE_TYPE"],
            "reading_ts": r["READING_TS"],
            "risk": r["RISK_24H"],
            "status": status_for(r["RISK_24H"]),
            "mtbf_h": r.get("MTBF_HOURS"),
            "mttr_h": r.get("MTTR_HOURS"),
            "failures": r.get("FAILURES"),
            "likely_modes": modes,
            "top_factors": top_factors(r),
            "signals": {col: {"value": r.get(col), "baseline": r.get(base), "z": r.get(zc)}
                        for zc, (col, base, _label, _unit) in SIGNALS.items()},
            "part": {"part_no": part_no, "on_hand": on_hand, "lead_time_days": p.get("LEAD_TIME_DAYS")},
            "unplanned_hours": unplanned_h,
            "planned_hours": planned_hours,
            "avoided_hours": avoided,
            "work_order": wo,
            "stage": stage,
        })
    return out
