-- Sensor fusion, step 1: COMBINE and COMPLETE.
--
-- Five source systems deliver the same machines' data with different IDs,
-- clocks, units, cadences, gaps and delays (see 01_schema.sql). This file turns
-- them into one complete, aligned, one-minute record per machine
-- (TELEMETRY_FUSED) and an hourly table for the analytics (TELEMETRY).
-- Every filled-in value is flagged, so later steps know what was measured.
OPEN SCHEMA OEE;

-- 0..199,999: a number table used to build time grids and expand intervals
CREATE OR REPLACE TABLE MINUTE_NUMBERS AS
SELECT LEVEL - 1 AS N FROM DUAL CONNECT BY LEVEL <= 200000;

-- ---------------------------------------------------------------- COMBINE --
-- Each source mapped to MACHINE_ID, UTC time and SI units.

CREATE OR REPLACE VIEW V_SRC_VIBRATION AS
SELECT r.MACHINE_ID, v.TS_UTC AS TS, v.VIB_RMS AS VIBRATION_MM_S
FROM SRC_CM_VIBRATION v
JOIN ASSET_REGISTRY r ON r.CM_SENSOR_ID = v.SENSOR_ID;

CREATE OR REPLACE VIEW V_SRC_TEMPS AS
SELECT r.MACHINE_ID,
       ADD_MINUTES(p.TS_LOCAL, -r.UTC_OFFSET_MIN) AS TS,          -- local clock -> UTC
       p.AIR_C + 273.15                           AS AIR_TEMP_K,  -- Celsius -> Kelvin
       p.PROCESS_C + 273.15                       AS PROCESS_TEMP_K
FROM SRC_PLC_TEMPS p
JOIN ASSET_REGISTRY r ON r.PLC_TAG = p.PLC_TAG;

CREATE OR REPLACE VIEW V_SRC_DRIVE AS
SELECT r.MACHINE_ID, d.TS_UTC AS TS, d.RPM, d.TORQUE_NM
FROM SRC_DRIVE d
JOIN ASSET_REGISTRY r ON r.DRIVE_ID = d.DRIVE_ID;

-- 15-minute energy totals -> average power for every minute of the interval,
-- counting only data that had arrived when the snapshot was taken
CREATE OR REPLACE VIEW V_SRC_POWER AS
SELECT r.MACHINE_ID,
       ADD_MINUTES(e.INTERVAL_START_UTC, n.N) AS TS,
       e.KWH * 60 / 15                        AS POWER_KW
FROM SRC_ENERGY e
JOIN ASSET_REGISTRY r ON r.METER_ID = e.METER_ID
JOIN MINUTE_NUMBERS n ON n.N < 15
CROSS JOIN FUSION_CONFIG c
WHERE e.RECEIVED_AT_UTC <= c.DATA_AS_OF;

CREATE OR REPLACE VIEW V_SRC_TOOL AS
SELECT r.MACHINE_ID,
       ADD_MINUTES(t.EVENT_TS_LOCAL, -r.UTC_OFFSET_MIN) AS TS,
       t.TOOL_WEAR_MIN
FROM SRC_MES_TOOL t
JOIN ASSET_REGISTRY r ON r.ASSET_TAG = t.ASSET_TAG;

-- One-minute time grid per machine over the period it reported, minus the
-- minutes it was stopped (recorded downtime)
CREATE OR REPLACE TABLE FUSION_SPINE AS
WITH bounds AS (
    SELECT MACHINE_ID, MIN(TS) AS T0, MAX(TS) AS T1
    FROM (SELECT MACHINE_ID, TS FROM V_SRC_DRIVE
          UNION ALL
          SELECT MACHINE_ID, TS FROM V_SRC_VIBRATION) x
    GROUP BY MACHINE_ID
),
grid AS (
    SELECT b.MACHINE_ID, ADD_MINUTES(b.T0, n.N) AS TS
    FROM bounds b
    JOIN MINUTE_NUMBERS n ON n.N <= MINUTES_BETWEEN(b.T1, b.T0)
)
SELECT g.MACHINE_ID, g.TS
FROM grid g
LEFT JOIN DOWNTIME d
  ON d.MACHINE_ID = g.MACHINE_ID AND g.TS >= d.START_TS AND g.TS < d.END_TS
WHERE d.STOP_ID IS NULL;

-- --------------------------------------------------------------- COMPLETE --
-- Align all streams on the grid, then fill short gaps by carrying the last
-- reading forward, within a limit per stream that matches how fast it changes.
--   vibration 15 min | temperatures 10 min | speed/torque 15 min
--   power 360 min (meter data arrives late) | tool wear 30 min
CREATE OR REPLACE TABLE TELEMETRY_FUSED AS
WITH aligned AS (
    SELECT s.MACHINE_ID, s.TS,
           v.VIBRATION_MM_S, t.AIR_TEMP_K, t.PROCESS_TEMP_K,
           d.RPM, d.TORQUE_NM, p.POWER_KW, w.TOOL_WEAR_MIN
    FROM FUSION_SPINE s
    LEFT JOIN V_SRC_VIBRATION v ON v.MACHINE_ID = s.MACHINE_ID AND v.TS = s.TS
    LEFT JOIN V_SRC_TEMPS     t ON t.MACHINE_ID = s.MACHINE_ID AND t.TS = s.TS
    LEFT JOIN V_SRC_DRIVE     d ON d.MACHINE_ID = s.MACHINE_ID AND d.TS = s.TS
    LEFT JOIN V_SRC_POWER     p ON p.MACHINE_ID = s.MACHINE_ID AND p.TS = s.TS
    LEFT JOIN V_SRC_TOOL      w ON w.MACHINE_ID = s.MACHINE_ID AND w.TS = s.TS
),
grouped AS (
    -- a new group starts at every real reading; the group carries that reading
    SELECT a.*,
           COUNT(VIBRATION_MM_S) OVER (PARTITION BY MACHINE_ID ORDER BY TS ROWS UNBOUNDED PRECEDING) AS G_VIB,
           COUNT(AIR_TEMP_K)     OVER (PARTITION BY MACHINE_ID ORDER BY TS ROWS UNBOUNDED PRECEDING) AS G_TMP,
           COUNT(RPM)            OVER (PARTITION BY MACHINE_ID ORDER BY TS ROWS UNBOUNDED PRECEDING) AS G_DRV,
           COUNT(POWER_KW)       OVER (PARTITION BY MACHINE_ID ORDER BY TS ROWS UNBOUNDED PRECEDING) AS G_PWR,
           COUNT(TOOL_WEAR_MIN)  OVER (PARTITION BY MACHINE_ID ORDER BY TS ROWS UNBOUNDED PRECEDING) AS G_TOOL
    FROM aligned a
),
filled AS (
    SELECT MACHINE_ID, TS,
           CASE WHEN VIBRATION_MM_S IS NOT NULL THEN 1 ELSE 0 END AS VIB_REAL,
           CASE WHEN AIR_TEMP_K IS NOT NULL THEN 1 ELSE 0 END AS TMP_REAL,
           CASE WHEN RPM IS NOT NULL THEN 1 ELSE 0 END AS DRV_REAL,
           CASE WHEN POWER_KW IS NOT NULL THEN 1 ELSE 0 END AS PWR_REAL,
           CASE WHEN TOOL_WEAR_MIN IS NOT NULL THEN 1 ELSE 0 END AS TOOL_REAL,
           MAX(VIBRATION_MM_S) OVER (PARTITION BY MACHINE_ID, G_VIB)               AS VIB_F,
           MINUTES_BETWEEN(TS, MIN(TS) OVER (PARTITION BY MACHINE_ID, G_VIB))      AS VIB_AGE,
           MAX(AIR_TEMP_K)     OVER (PARTITION BY MACHINE_ID, G_TMP)               AS AIR_F,
           MAX(PROCESS_TEMP_K) OVER (PARTITION BY MACHINE_ID, G_TMP)               AS PROC_F,
           MINUTES_BETWEEN(TS, MIN(TS) OVER (PARTITION BY MACHINE_ID, G_TMP))      AS TMP_AGE,
           MAX(RPM)            OVER (PARTITION BY MACHINE_ID, G_DRV)               AS RPM_F,
           MAX(TORQUE_NM)      OVER (PARTITION BY MACHINE_ID, G_DRV)               AS TRQ_F,
           MINUTES_BETWEEN(TS, MIN(TS) OVER (PARTITION BY MACHINE_ID, G_DRV))      AS DRV_AGE,
           MAX(POWER_KW)       OVER (PARTITION BY MACHINE_ID, G_PWR)               AS PWR_F,
           MINUTES_BETWEEN(TS, MIN(TS) OVER (PARTITION BY MACHINE_ID, G_PWR))      AS PWR_AGE,
           MAX(TOOL_WEAR_MIN)  OVER (PARTITION BY MACHINE_ID, G_TOOL)              AS TOOL_F,
           MINUTES_BETWEEN(TS, MIN(TS) OVER (PARTITION BY MACHINE_ID, G_TOOL))     AS TOOL_AGE,
           G_VIB, G_TMP, G_DRV, G_PWR, G_TOOL
    FROM grouped
)
SELECT MACHINE_ID,
       TS                                                                AS READING_TS,
       CASE WHEN G_TMP  > 0 AND TMP_AGE  <= 10  THEN AIR_F  END          AS AIR_TEMP_K,
       CASE WHEN G_TMP  > 0 AND TMP_AGE  <= 10  THEN PROC_F END          AS PROCESS_TEMP_K,
       CASE WHEN G_DRV  > 0 AND DRV_AGE  <= 15  THEN RPM_F  END          AS RPM,
       CASE WHEN G_DRV  > 0 AND DRV_AGE  <= 15  THEN TRQ_F  END          AS TORQUE_NM,
       CASE WHEN G_TOOL > 0 AND TOOL_AGE <= 30  THEN TOOL_F END          AS TOOL_WEAR_MIN,
       CASE WHEN G_VIB  > 0 AND VIB_AGE  <= 15  THEN VIB_F  END          AS VIBRATION_MM_S,
       CASE WHEN G_PWR  > 0 AND PWR_AGE  <= 360 THEN PWR_F  END          AS POWER_KW,
       -- 1 = measured at this minute, 0 = filled in, NULL = still missing
       CASE WHEN VIB_REAL = 1 THEN 1 WHEN G_VIB  > 0 AND VIB_AGE  <= 15  THEN 0 END AS VIB_Q,
       CASE WHEN TMP_REAL = 1 THEN 1 WHEN G_TMP  > 0 AND TMP_AGE  <= 10  THEN 0 END AS TMP_Q,
       CASE WHEN DRV_REAL = 1 THEN 1 WHEN G_DRV  > 0 AND DRV_AGE  <= 15  THEN 0 END AS DRV_Q,
       CASE WHEN PWR_REAL = 1 THEN 1 WHEN G_PWR  > 0 AND PWR_AGE  <= 360 THEN 0 END AS PWR_Q,
       CASE WHEN TOOL_REAL = 1 THEN 1 WHEN G_TOOL > 0 AND TOOL_AGE <= 30  THEN 0 END AS TOOL_Q
FROM filled;

-- Hourly table for the analytics and the model: hours with at least 30
-- usable minutes, plus the share of values that were actually measured
CREATE OR REPLACE TABLE TELEMETRY AS
SELECT MACHINE_ID,
       DATE_TRUNC('HOUR', READING_TS)  AS READING_TS,
       AVG(AIR_TEMP_K)                 AS AIR_TEMP_K,
       AVG(PROCESS_TEMP_K)             AS PROCESS_TEMP_K,
       AVG(RPM)                        AS RPM,
       AVG(TORQUE_NM)                  AS TORQUE_NM,
       AVG(TOOL_WEAR_MIN)              AS TOOL_WEAR_MIN,
       AVG(VIBRATION_MM_S)             AS VIBRATION_MM_S,
       AVG(POWER_KW)                   AS POWER_KW,
       COUNT(*)                        AS MINUTES,
       (SUM(COALESCE(VIB_Q, 0)) + SUM(COALESCE(TMP_Q, 0)) + SUM(COALESCE(DRV_Q, 0))
        + SUM(COALESCE(PWR_Q, 0)) + SUM(COALESCE(TOOL_Q, 0))) / (5.0 * COUNT(*)) AS MEASURED_SHARE
FROM TELEMETRY_FUSED
GROUP BY MACHINE_ID, DATE_TRUNC('HOUR', READING_TS)
HAVING COUNT(*) >= 30
   AND AVG(AIR_TEMP_K) IS NOT NULL AND AVG(RPM) IS NOT NULL AND AVG(TOOL_WEAR_MIN) IS NOT NULL
   AND AVG(VIBRATION_MM_S) IS NOT NULL AND AVG(POWER_KW) IS NOT NULL;

-- What each source delivered, and what the fusion did with it
CREATE OR REPLACE VIEW V_SOURCE_HEALTH AS
WITH per_stream AS (
    SELECT 'Condition monitor' AS SOURCE_NAME, 'Vibration' AS STREAM, 1 AS ORD,
           SUM(CASE WHEN VIB_Q = 1 THEN 1 ELSE 0 END) AS MEASURED,
           SUM(CASE WHEN VIB_Q = 0 THEN 1 ELSE 0 END) AS FILLED,
           SUM(CASE WHEN VIB_Q IS NULL THEN 1 ELSE 0 END) AS MISSING, COUNT(*) AS GRID
    FROM TELEMETRY_FUSED
    UNION ALL
    SELECT 'PLC historian', 'Temperatures', 2,
           SUM(CASE WHEN TMP_Q = 1 THEN 1 ELSE 0 END), SUM(CASE WHEN TMP_Q = 0 THEN 1 ELSE 0 END),
           SUM(CASE WHEN TMP_Q IS NULL THEN 1 ELSE 0 END), COUNT(*)
    FROM TELEMETRY_FUSED
    UNION ALL
    SELECT 'Drive controller', 'Speed and torque', 3,
           SUM(CASE WHEN DRV_Q = 1 THEN 1 ELSE 0 END), SUM(CASE WHEN DRV_Q = 0 THEN 1 ELSE 0 END),
           SUM(CASE WHEN DRV_Q IS NULL THEN 1 ELSE 0 END), COUNT(*)
    FROM TELEMETRY_FUSED
    UNION ALL
    SELECT 'Energy meter', 'Power', 4,
           SUM(CASE WHEN PWR_Q = 1 THEN 1 ELSE 0 END), SUM(CASE WHEN PWR_Q = 0 THEN 1 ELSE 0 END),
           SUM(CASE WHEN PWR_Q IS NULL THEN 1 ELSE 0 END), COUNT(*)
    FROM TELEMETRY_FUSED
    UNION ALL
    SELECT 'Production system', 'Tool wear', 5,
           SUM(CASE WHEN TOOL_Q = 1 THEN 1 ELSE 0 END), SUM(CASE WHEN TOOL_Q = 0 THEN 1 ELSE 0 END),
           SUM(CASE WHEN TOOL_Q IS NULL THEN 1 ELSE 0 END), COUNT(*)
    FROM TELEMETRY_FUSED
),
raw_rows AS (
    SELECT 1 AS ORD, COUNT(*) AS RAW_ROWS, 'sensor ID, 1 min, UTC'                 AS SOURCE_FORMAT, CAST(NULL AS DOUBLE) AS AVG_DELAY_MIN FROM SRC_CM_VIBRATION
    UNION ALL SELECT 2, COUNT(*), 'PLC tag, 5 min, local time, deg C', NULL FROM SRC_PLC_TEMPS
    UNION ALL SELECT 3, COUNT(*), 'drive ID, 1 min, UTC', NULL FROM SRC_DRIVE
    UNION ALL SELECT 4, COUNT(*), 'meter ID, 15-min kWh, delivered late',
                     AVG(MINUTES_BETWEEN(RECEIVED_AT_UTC, ADD_MINUTES(INTERVAL_START_UTC, 15))) FROM SRC_ENERGY
    UNION ALL SELECT 5, COUNT(*), 'asset tag, irregular events, local time', NULL FROM SRC_MES_TOOL
)
SELECT p.ORD, p.SOURCE_NAME, p.STREAM, r.SOURCE_FORMAT, r.RAW_ROWS, r.AVG_DELAY_MIN,
       p.GRID, p.MEASURED, p.FILLED, p.MISSING,
       p.MEASURED * 1.0 / p.GRID AS MEASURED_SHARE,
       (p.MEASURED + p.FILLED) * 1.0 / p.GRID AS COMPLETE_SHARE
FROM per_stream p
JOIN raw_rows r ON r.ORD = p.ORD;

-- The same raw sources, joined the way a quick script would: on the
-- timestamps each system reports, no time-zone or unit conversion, no gap
-- filling. Used only to show what the fusion step is worth (08_fusion_impact.sql).
CREATE OR REPLACE TABLE TELEMETRY_NAIVE AS
WITH m AS (
    SELECT r.MACHINE_ID, d.TS_UTC AS TS, d.RPM, d.TORQUE_NM
    FROM SRC_DRIVE d JOIN ASSET_REGISTRY r ON r.DRIVE_ID = d.DRIVE_ID
)
SELECT m.MACHINE_ID,
       DATE_TRUNC('HOUR', m.TS)   AS READING_TS,
       AVG(p.AIR_C)               AS AIR_TEMP_K,        -- Celsius taken as Kelvin
       AVG(p.PROCESS_C)           AS PROCESS_TEMP_K,
       AVG(m.RPM)                 AS RPM,
       AVG(m.TORQUE_NM)           AS TORQUE_NM,
       AVG(t.TOOL_WEAR_MIN)       AS TOOL_WEAR_MIN,
       AVG(v.VIB_RMS)             AS VIBRATION_MM_S,
       AVG(e.KWH)                 AS POWER_KW,          -- kWh per 15 min taken as kW
       COUNT(*)                   AS MINUTES,
       CAST(NULL AS DOUBLE)       AS MEASURED_SHARE
FROM m
JOIN ASSET_REGISTRY r ON r.MACHINE_ID = m.MACHINE_ID
LEFT JOIN SRC_CM_VIBRATION v ON v.SENSOR_ID = r.CM_SENSOR_ID AND v.TS_UTC = m.TS
LEFT JOIN SRC_PLC_TEMPS    p ON p.PLC_TAG   = r.PLC_TAG      AND p.TS_LOCAL = m.TS          -- local clock read as UTC
LEFT JOIN SRC_ENERGY       e ON e.METER_ID  = r.METER_ID     AND e.INTERVAL_START_UTC = m.TS
LEFT JOIN SRC_MES_TOOL     t ON t.ASSET_TAG = r.ASSET_TAG    AND t.EVENT_TS_LOCAL = m.TS    -- local clock read as UTC
GROUP BY m.MACHINE_ID, DATE_TRUNC('HOUR', m.TS)
HAVING COUNT(*) >= 30
   AND AVG(p.AIR_C) IS NOT NULL AND AVG(t.TOOL_WEAR_MIN) IS NOT NULL
   AND AVG(v.VIB_RMS) IS NOT NULL AND AVG(e.KWH) IS NOT NULL;
