"""Live demo: search a fault's multi-stream pattern across the whole sensor history.

A fault leaves a pattern across the five streams (sql/06_root_cause.sql learns it
as FAULT_SIGNATURES). Here Exasol scans every reading of the fleet-scale history
(OEE_SCALE.FEATURES, ~14 M one-minute readings after `python -m oee.cli bench`;
the demo plant's hourly readings if the bench has not been run), scores each
reading against all five signatures, keeps the readings where the selected fault
is the closest pattern AND its own stream is at least ALARM_Z past normal, groups
them into episodes that last at least MIN_HOURS, and checks each episode against
the failures that followed within FOLLOW_HOURS.

The replay picks a held-out failure of the same kind in the demo plant and steps
through the 48 hours before it, hour by hour, so the same pattern can be watched
forming in a live stream until the root cause selects that fault.
"""
from __future__ import annotations

MODES = {
    # failure mode: (button label, stream that must be past its alarm level)
    "BEARING": ("Bearing", "VIB"),
    "HEAT_DISSIPATION": ("Coolant", "TEMP"),
    "OVERSTRAIN": ("Overstrain", "TORQ"),
    "POWER": ("Drive", "PWR"),
}
STREAMS = [("VIB", "VIBRATION"), ("TEMP", "TEMPERATURE"), ("TORQ", "TORQUE"), ("PWR", "POWER"), ("TOOL", "TOOL")]
ALL_MODES = ["TOOL_WEAR", "HEAT_DISSIPATION", "POWER", "OVERSTRAIN", "BEARING"]
ALARM_Z = 2.5             # own stream must be this far past normal (sigma)
MIN_HOURS = 3             # and the pattern must hold this long
EPISODE_GAP_MIN = 30       # matched one-minute readings further apart than this start a new episode
FOLLOW_HOURS = 48          # an episode "came true" if that failure followed within this


def source_tables(scale: bool) -> dict:
    if scale:
        return {"features": "OEE_SCALE.FEATURES", "machines": "OEE_SCALE.MACHINES",
                "downtime": "OEE_SCALE.DOWNTIME", "torque_window": 60, "gap_min": EPISODE_GAP_MIN, "per_hour": 60,
                "label": "fleet-scale history (OEE_SCALE)"}
    return {"features": "OEE.V_FEATURES", "machines": "OEE.MACHINES",
            "downtime": "OEE.DOWNTIME", "torque_window": 1, "gap_min": 90, "per_hour": 1,
            "label": "demo plant history (OEE)"}


def search_sql(mode: str, scale: bool) -> str:
    """CREATE TABLE OEE.PATTERN_EPISODES: every episode where `mode`'s pattern shows."""
    if mode not in MODES:
        raise ValueError(mode)
    t = source_tables(scale)
    k = t["torque_window"]
    sig_cols = ",\n       ".join(
        f"MAX(CASE WHEN FAILURE_MODE = '{m}' AND STREAM = '{s}' THEN SIGNATURE_Z END) AS S_{i}_{a}"
        for i, m in enumerate(ALL_MODES) for a, s in STREAMS)
    dist = {i: " + ".join(f"POWER(z.Z_{a} - COALESCE(s.S_{i}_{a}, 0), 2)" for a, _ in STREAMS)
            for i in range(len(ALL_MODES))}
    sel = ALL_MODES.index(mode)
    own = MODES[mode][1]
    others = ", ".join(f"D_{i}" for i in range(len(ALL_MODES)))
    return f"""CREATE OR REPLACE TABLE OEE.PATTERN_EPISODES AS
WITH f AS (
    SELECT f.MACHINE_ID, m.MACHINE_TYPE, f.READING_TS,
           f.VIB_AVG_6H        AS VIB,
           f.TEMP_DIFF_AVG_6H  AS TEMP,
           AVG(f.TORQUE_RATIO) OVER (PARTITION BY f.MACHINE_ID ORDER BY f.READING_TS
                                     ROWS BETWEEN {k - 1} PRECEDING AND CURRENT ROW) AS TORQ,
           f.POWER_AVG_6H      AS PWR,
           f.TOOL_WEAR_MIN     AS TOOL
    FROM {t["features"]} f
    JOIN {t["machines"]} m ON m.MACHINE_ID = f.MACHINE_ID
),
normals AS (
    SELECT MACHINE_TYPE,
           AVG(VIB) AS VIB_MU,   STDDEV(VIB) AS VIB_SD,
           AVG(TEMP) AS TEMP_MU, STDDEV(TEMP) AS TEMP_SD,
           AVG(TORQ) AS TORQ_MU, STDDEV(TORQ) AS TORQ_SD,
           AVG(PWR) AS PWR_MU,   STDDEV(PWR) AS PWR_SD,
           AVG(TOOL) AS TOOL_MU, STDDEV(TOOL) AS TOOL_SD
    FROM f
    GROUP BY MACHINE_TYPE
),
z AS (
    SELECT f.MACHINE_ID, f.READING_TS,
           COALESCE((f.VIB - n.VIB_MU) / NULLIF(n.VIB_SD, 0), 0)        AS Z_VIB,
           COALESCE(-(f.TEMP - n.TEMP_MU) / NULLIF(n.TEMP_SD, 0), 0)    AS Z_TEMP,
           COALESCE((f.TORQ - n.TORQ_MU) / NULLIF(n.TORQ_SD, 0), 0)     AS Z_TORQ,
           COALESCE(ABS(f.PWR - n.PWR_MU) / NULLIF(n.PWR_SD, 0), 0)     AS Z_PWR,
           COALESCE((f.TOOL - n.TOOL_MU) / NULLIF(n.TOOL_SD, 0), 0)     AS Z_TOOL
    FROM f
    JOIN normals n ON n.MACHINE_TYPE = f.MACHINE_TYPE
),
sig AS (
    SELECT {sig_cols}
    FROM OEE.FAULT_SIGNATURES
),
scored AS (
    SELECT z.MACHINE_ID, z.READING_TS, z.Z_{own} AS OWN_Z,
           {", ".join(f"{dist[i]} AS D_{i}" for i in range(len(ALL_MODES)))}
    FROM z
    CROSS JOIN sig s
),
hits AS (
    SELECT MACHINE_ID, READING_TS, OWN_Z
    FROM scored
    WHERE D_{sel} = LEAST({others}) AND OWN_Z >= {ALARM_Z}
),
marked AS (
    SELECT h.*,
           CASE WHEN MINUTES_BETWEEN(READING_TS,
                     LAG(READING_TS) OVER (PARTITION BY MACHINE_ID ORDER BY READING_TS)) <= {t["gap_min"]}
                THEN 0 ELSE 1 END AS NEW_EPISODE
    FROM hits h
),
numbered AS (
    SELECT m.*,
           SUM(NEW_EPISODE) OVER (PARTITION BY MACHINE_ID ORDER BY READING_TS
                                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS EPISODE_NO
    FROM marked m
)
SELECT '{mode}' AS FAILURE_MODE, MACHINE_ID, EPISODE_NO,
       MIN(READING_TS) AS FIRST_TS, MAX(READING_TS) AS LAST_TS,
       COUNT(*) AS READINGS, MAX(OWN_Z) AS PEAK_Z
FROM numbered
GROUP BY MACHINE_ID, EPISODE_NO
HAVING COUNT(*) >= {MIN_HOURS * t["per_hour"]}"""


def summary_sql(mode: str, scale: bool) -> str:
    """How many episodes were followed by that failure, and how many such failures were warned.

    An episode "came true" if that failure happened after it began and no more than
    FOLLOW_HOURS after it ended; a failure was "warned" if an episode began before it
    and was still active within FOLLOW_HOURS of it."""
    d = source_tables(scale)["downtime"]
    return f"""WITH came_true AS (
    SELECT DISTINCT e.MACHINE_ID, e.EPISODE_NO
    FROM OEE.PATTERN_EPISODES e
    JOIN {d} s
      ON  s.MACHINE_ID = e.MACHINE_ID
      AND s.STOP_TYPE = 'UNPLANNED' AND s.FAILURE_MODE = '{mode}'
      AND s.START_TS >= e.FIRST_TS
      AND s.START_TS <= ADD_HOURS(e.LAST_TS, {FOLLOW_HOURS})
),
stops AS (
    SELECT s.STOP_ID, s.MACHINE_ID, s.START_TS
    FROM {d} s
    WHERE s.STOP_TYPE = 'UNPLANNED' AND s.FAILURE_MODE = '{mode}'
),
warned AS (
    SELECT DISTINCT s.STOP_ID
    FROM stops s
    JOIN OEE.PATTERN_EPISODES e
      ON  e.MACHINE_ID = s.MACHINE_ID
      AND e.FIRST_TS <= s.START_TS
      AND e.LAST_TS >= ADD_HOURS(s.START_TS, -{FOLLOW_HOURS})
),
ep AS (
    SELECT COUNT(*) AS EPISODES, COUNT(DISTINCT MACHINE_ID) AS MACHINES,
           COALESCE(SUM(READINGS), 0) AS MATCHED_READINGS
    FROM OEE.PATTERN_EPISODES
),
ct AS (SELECT COUNT(*) AS EPISODES_CAME_TRUE FROM came_true),
st AS (SELECT COUNT(*) AS FAILURES FROM stops),
wn AS (SELECT COUNT(*) AS FAILURES_WARNED FROM warned)
SELECT ep.EPISODES, ep.MACHINES, ep.MATCHED_READINGS, ct.EPISODES_CAME_TRUE, st.FAILURES, wn.FAILURES_WARNED
FROM ep CROSS JOIN ct CROSS JOIN st CROSS JOIN wn"""


def replay_sql(mode: str) -> tuple[str, str]:
    """A held-out failure of `mode` in the demo plant that the root cause named, and its last 48 hours."""
    pick = f"""SELECT r.STOP_ID, d.MACHINE_ID, d.START_TS, m.PLANT_ID, m.MACHINE_TYPE, r.FIRST_CORRECT_HOURS_BEFORE
FROM OEE.ROOT_CAUSE_EVAL r
JOIN OEE.DOWNTIME d ON d.STOP_ID = r.STOP_ID
JOIN OEE.MACHINES m ON m.MACHINE_ID = d.MACHINE_ID
WHERE r.TRUE_MODE = '{mode}' AND r.CORRECT = 1
ORDER BY r.FIRST_CORRECT_HOURS_BEFORE DESC, r.STOP_ID
LIMIT 10"""
    stream = """SELECT p.READING_TS,
       MAX(CASE WHEN p.STREAM = 'VIBRATION'   THEN p.BAD_Z END) AS VIB,
       MAX(CASE WHEN p.STREAM = 'TEMPERATURE' THEN p.BAD_Z END) AS TEMP,
       MAX(CASE WHEN p.STREAM = 'TORQUE'      THEN p.BAD_Z END) AS TORQ,
       MAX(CASE WHEN p.STREAM = 'POWER'       THEN p.BAD_Z END) AS PWR,
       MAX(CASE WHEN p.STREAM = 'TOOL'        THEN p.BAD_Z END) AS TOOL,
       MAX(CASE WHEN r.CAUSE_RANK = 1 THEN r.FAILURE_MODE END)  AS TOP_MODE,
       MAX(CASE WHEN r.CAUSE_RANK = 1 THEN r.CONFIDENCE END)    AS TOP_CONFIDENCE,
       MAX(s.RISK_24H)                                          AS RISK_24H
FROM OEE.STREAM_PATTERNS p
JOIN OEE.ROOT_CAUSE r ON r.MACHINE_ID = p.MACHINE_ID AND r.READING_TS = p.READING_TS
LEFT JOIN OEE.RISK_SCORES s ON s.MACHINE_ID = p.MACHINE_ID AND s.READING_TS = p.READING_TS
WHERE p.MACHINE_ID = '{machine}'
  AND p.READING_TS <  TIMESTAMP '{stop_ts}'
  AND p.READING_TS >= ADD_HOURS(TIMESTAMP '{stop_ts}', -48)
GROUP BY p.READING_TS
ORDER BY p.READING_TS"""
    return pick, stream


def selection_hour(rows: list[dict], mode: str) -> int | None:
    """Index of the reading where the search rule first holds (same rule as search_sql)."""
    own = MODES[mode][1]
    run = 0
    for i, r in enumerate(rows):
        run = run + 1 if r.get("TOP_MODE") == mode and (r.get(own) or 0) >= ALARM_Z else 0
        if run >= MIN_HOURS:
            return i
    return None


# ------------------------------------------------------------ step 3: the kit ----

def local_parts_sql(mode: str, plant: str) -> str:
    """Search only the plant's own store system (database 1) for the repair kit."""
    from oee.stores import STORE_SYSTEMS

    table, _system, col, _fmt = STORE_SYSTEMS[plant]
    qty_col = {"MATNR": "LABST", "ITEM_NAME": "CLOSING_QTY", "SKU": "QTY_AVAILABLE"}[col]
    key = {"MATNR": "UPPER(TRIM(s.MATNR))", "ITEM_NAME": "UPPER(TRIM(s.ITEM_NAME))",
           "SKU": "REPLACE(UPPER(TRIM(s.SKU)), '_', '-')"}[col]
    return f"""WITH kit AS (
    SELECT PART_NO, QTY FROM OEE.REPAIR_KITS WHERE FAILURE_MODE = '{mode}'
),
reserved AS (
    SELECT PART_NO, SUM(QTY) AS QTY FROM OEE.PART_RESERVATIONS
    WHERE STATUS = 'RESERVED' AND FROM_LOCATION = '{plant}'
    GROUP BY PART_NO
),
here AS (
    SELECT {key} AS PART_NO, SUM(s.{qty_col}) AS ON_HAND
    FROM {table} s
    GROUP BY {key}
)
SELECT k.PART_NO, k.QTY AS NEEDED,
       COALESCE(h.ON_HAND, 0) AS ON_HAND,
       COALESCE(h.ON_HAND, 0) - COALESCE(r.QTY, 0) AS FREE_QTY,
       CASE WHEN COALESCE(h.ON_HAND, 0) - COALESCE(r.QTY, 0) >= k.QTY THEN 'FOUND'
            WHEN COALESCE(h.ON_HAND, 0) - COALESCE(r.QTY, 0) > 0 THEN 'SHORT'
            ELSE 'MISSING' END AS LOCAL_STATUS
FROM kit k
LEFT JOIN here h     ON h.PART_NO = k.PART_NO
LEFT JOIN reserved r ON r.PART_NO = k.PART_NO
ORDER BY k.PART_NO"""


def network_parts_sql(mode: str, plant: str) -> str:
    """Search every store system for each part of the kit; rank the sources by when the part can be on site."""
    return f"""WITH kit AS (
    SELECT PART_NO, QTY FROM OEE.REPAIR_KITS WHERE FAILURE_MODE = '{mode}'
),
reserved AS (
    SELECT FROM_LOCATION, PART_NO, SUM(QTY) AS QTY FROM OEE.PART_RESERVATIONS
    WHERE STATUS = 'RESERVED' AND FROM_LOCATION IS NOT NULL
    GROUP BY FROM_LOCATION, PART_NO
),
stock AS (
    SELECT s.LOCATION_ID, s.SOURCE_SYSTEM, s.BIN_LOCATION, s.PART_NO,
           s.ON_HAND - COALESCE(r.QTY, 0) AS FREE_QTY, s.LEAD_TIME_DAYS
    FROM OEE.V_STORE_STOCK s
    LEFT JOIN reserved r ON r.FROM_LOCATION = s.LOCATION_ID AND r.PART_NO = s.PART_NO
),
found_stock AS (
    SELECT k.PART_NO, k.QTY AS NEEDED, st.LOCATION_ID, st.SOURCE_SYSTEM, st.BIN_LOCATION, st.FREE_QTY,
           CASE WHEN st.LOCATION_ID = '{plant}' THEN 0.0 ELSE l.TRANSFER_HOURS * 1.0 END AS ETA_HOURS,
           CASE WHEN st.LOCATION_ID = '{plant}' THEN 'LOCAL' ELSE 'TRANSFER' END AS SOURCE_KIND
    FROM kit k
    JOIN stock st ON st.PART_NO = k.PART_NO
    LEFT JOIN OEE.TRANSFER_LANES l ON l.FROM_LOCATION = st.LOCATION_ID AND l.TO_LOCATION = '{plant}'
),
supplier AS (
    SELECT k.PART_NO, k.QTY AS NEEDED, CAST(NULL AS VARCHAR(8)) AS LOCATION_ID,
           'supplier' AS SOURCE_SYSTEM, CAST(NULL AS VARCHAR(16)) AS BIN_LOCATION,
           CAST(NULL AS DECIMAL(10,0)) AS FREE_QTY, st.LEAD_TIME_DAYS * 24.0 AS ETA_HOURS,
           'SUPPLIER' AS SOURCE_KIND
    FROM kit k
    JOIN stock st ON st.PART_NO = k.PART_NO AND st.LOCATION_ID = '{plant}'
),
kit_options AS (
    SELECT * FROM found_stock
    UNION ALL
    SELECT * FROM supplier
)
SELECT o.*,
       CASE WHEN o.SOURCE_KIND = 'SUPPLIER' OR o.FREE_QTY >= o.NEEDED THEN 1 ELSE 0 END AS CAN_SUPPLY,
       ROW_NUMBER() OVER (PARTITION BY o.PART_NO
                          ORDER BY CASE WHEN o.SOURCE_KIND = 'SUPPLIER' OR o.FREE_QTY >= o.NEEDED THEN 0 ELSE 1 END,
                                   o.ETA_HOURS) AS OPTION_RANK
FROM kit_options o
ORDER BY o.PART_NO, o.ETA_HOURS"""


# ------------------------------------------------------- step 7: order a part ----

def plain_search_sql(part: str) -> str:
    """What a normal lookup does: look for our part number in the supplier files."""
    return f"""SELECT SUPPLIER_ID, SUPPLIER_CODE, DESCRIPTION
FROM OEE.V_SUPPLIER_OFFERS
WHERE UPPER(SUPPLIER_CODE) = '{part}' OR INSTR(UPPER(DESCRIPTION), '{part}') > 0"""


def deep_scan_sql(part: str) -> str:
    """Read every supplier line in its own format, normalise it, and judge it against our part."""
    return f"""WITH p AS (
    SELECT PART_NO, CATEGORY, MATCH_KEY FROM OEE.PART_MASTER WHERE PART_NO = '{part}'
),
cand AS (
    SELECT DISTINCT t.SUPPLIER_ID, t.SUPPLIER_CODE, t.DESCRIPTION, t.RAW_LINE, t.SQUASHED,
           t.UNIT_PRICE_INR, t.LEAD_DAYS, t.ORDER_MULTIPLE, p.MATCH_KEY,
           CASE WHEN INSTR(t.SQUASHED, p.MATCH_KEY) > 0 THEN 0
                ELSE LEAST(COALESCE(EDIT_DISTANCE(t.TOKEN_1, p.MATCH_KEY), 99),
                           COALESCE(EDIT_DISTANCE(t.TOKEN_2, p.MATCH_KEY), 99),
                           COALESCE(EDIT_DISTANCE(t.TOKEN_3, p.MATCH_KEY), 99)) END AS DIST
    FROM OEE.V_OFFER_TEXT t
    CROSS JOIN p
    JOIN OEE.CATEGORY_TERMS c ON c.CATEGORY = p.CATEGORY AND INSTR(t.UP, c.TERM) > 0
)
SELECT c.SUPPLIER_ID, s.NAME AS SUPPLIER_NAME, s.CATALOG_FORMAT, s.CURRENCY,
       c.SUPPLIER_CODE, c.DESCRIPTION, c.RAW_LINE, c.SQUASHED, c.MATCH_KEY, c.DIST,
       c.UNIT_PRICE_INR, c.LEAD_DAYS, c.ORDER_MULTIPLE,
       CASE WHEN c.DIST = 0 THEN 'SAME'
            WHEN LENGTH(c.MATCH_KEY) >= 8 AND c.DIST <= 2 THEN 'ASK'
            WHEN LENGTH(c.MATCH_KEY) >= 6 AND c.DIST <= 1 THEN 'ASK'
            ELSE 'DIFFERENT' END AS VERDICT
FROM cand c
JOIN OEE.SUPPLIERS s ON s.SUPPLIER_ID = c.SUPPLIER_ID
ORDER BY c.SUPPLIER_ID, c.DIST, c.SUPPLIER_CODE"""


# ------------------------------------------------- step 0: combine the sources ----

def fusion_demo_statements() -> list[str]:
    """The real COMBINE + COMPLETE statements from sql/02_fusion.sql (time grid and the
    aligned, gap-filled one-minute table), writing to DEMO_* copies so the demo button
    never touches the tables the rest of the pipeline uses."""
    from pathlib import Path

    from oee.sqlrun import split_statements

    text = (Path(__file__).resolve().parents[1] / "sql" / "02_fusion.sql").read_text()
    out = ["OPEN SCHEMA OEE"]
    for st in split_statements(text):
        if "TABLE FUSION_SPINE AS" in st or "TABLE TELEMETRY_FUSED AS" in st:
            st = st.replace("TABLE FUSION_SPINE AS", "TABLE DEMO_FUSION_SPINE AS")
            st = st.replace("FROM FUSION_SPINE s", "FROM DEMO_FUSION_SPINE s")
            st = st.replace("TABLE TELEMETRY_FUSED AS", "TABLE DEMO_TELEMETRY_FUSED AS")
            out.append(st)
    return out


def source_window_sql(machine: str, t0: str, t1: str) -> dict[str, str]:
    """Each source system's own rows for one machine around [t0, t1] (UTC), in its own format."""
    return {
        "registry": f"""SELECT MACHINE_ID, CM_SENSOR_ID, PLC_TAG, DRIVE_ID, METER_ID, ASSET_TAG, UTC_OFFSET_MIN
FROM OEE.ASSET_REGISTRY WHERE MACHINE_ID = '{machine}'""",
        "vibration": f"""SELECT v.SENSOR_ID, v.TS_UTC, v.VIB_RMS
FROM OEE.SRC_CM_VIBRATION v JOIN OEE.ASSET_REGISTRY r ON r.CM_SENSOR_ID = v.SENSOR_ID
WHERE r.MACHINE_ID = '{machine}' AND v.TS_UTC BETWEEN TIMESTAMP '{t0}' AND TIMESTAMP '{t1}'
ORDER BY v.TS_UTC""",
        "temps": f"""SELECT p.PLC_TAG, p.TS_LOCAL, p.AIR_C, p.PROCESS_C
FROM OEE.SRC_PLC_TEMPS p JOIN OEE.ASSET_REGISTRY r ON r.PLC_TAG = p.PLC_TAG
WHERE r.MACHINE_ID = '{machine}'
  AND ADD_MINUTES(p.TS_LOCAL, -r.UTC_OFFSET_MIN) BETWEEN TIMESTAMP '{t0}' AND TIMESTAMP '{t1}'
ORDER BY p.TS_LOCAL""",
        "drive": f"""SELECT d.DRIVE_ID, d.TS_UTC, d.RPM, d.TORQUE_NM
FROM OEE.SRC_DRIVE d JOIN OEE.ASSET_REGISTRY r ON r.DRIVE_ID = d.DRIVE_ID
WHERE r.MACHINE_ID = '{machine}' AND d.TS_UTC BETWEEN TIMESTAMP '{t0}' AND TIMESTAMP '{t1}'
ORDER BY d.TS_UTC""",
        "energy": f"""SELECT e.METER_ID, e.INTERVAL_START_UTC, e.KWH, e.RECEIVED_AT_UTC
FROM OEE.SRC_ENERGY e JOIN OEE.ASSET_REGISTRY r ON r.METER_ID = e.METER_ID
WHERE r.MACHINE_ID = '{machine}' AND e.INTERVAL_START_UTC <= TIMESTAMP '{t1}'
ORDER BY e.INTERVAL_START_UTC DESC
LIMIT 3""",
        "tool": f"""SELECT t.ASSET_TAG, t.EVENT_TS_LOCAL, t.TOOL_WEAR_MIN
FROM OEE.SRC_MES_TOOL t JOIN OEE.ASSET_REGISTRY r ON r.ASSET_TAG = t.ASSET_TAG
WHERE r.MACHINE_ID = '{machine}'
  AND ADD_MINUTES(t.EVENT_TS_LOCAL, -r.UTC_OFFSET_MIN) <= TIMESTAMP '{t1}'
ORDER BY t.EVENT_TS_LOCAL DESC
LIMIT 3""",
    }


def fused_window_sql(machine: str, t0: str, t1: str) -> str:
    return f"""SELECT READING_TS, VIBRATION_MM_S, VIB_Q, AIR_TEMP_K, PROCESS_TEMP_K, TMP_Q,
       RPM, TORQUE_NM, DRV_Q, POWER_KW, PWR_Q, TOOL_WEAR_MIN, TOOL_Q
FROM OEE.DEMO_TELEMETRY_FUSED
WHERE MACHINE_ID = '{machine}' AND READING_TS BETWEEN TIMESTAMP '{t0}' AND TIMESTAMP '{t1}'
ORDER BY READING_TS"""
