-- Feature engineering and labels, computed in-database with window functions.
-- Must stay in sync with oee/features.py (the offline reference used in tests).
OPEN SCHEMA OEE;

CREATE OR REPLACE VIEW V_FEATURES AS
SELECT
    t.MACHINE_ID,
    m.PLANT_ID,
    m.MACHINE_TYPE,
    t.READING_TS,
    t.AIR_TEMP_K,
    t.PROCESS_TEMP_K,
    t.PROCESS_TEMP_K - t.AIR_TEMP_K                                   AS TEMP_DIFF_K,
    t.RPM,
    t.TORQUE_NM,
    t.TORQUE_NM / m.NOMINAL_TORQUE                                    AS TORQUE_RATIO,
    t.TOOL_WEAR_MIN,
    t.TOOL_WEAR_MIN * t.TORQUE_NM / m.NOMINAL_TORQUE                  AS WEAR_X_LOAD,
    t.VIBRATION_MM_S,
    t.POWER_KW,
    AVG(t.VIBRATION_MM_S) OVER (
        PARTITION BY t.MACHINE_ID ORDER BY t.READING_TS
        ROWS BETWEEN 5 PRECEDING AND CURRENT ROW)                     AS VIB_AVG_6H,
    t.VIBRATION_MM_S - COALESCE(
        LAG(t.VIBRATION_MM_S, 6) OVER (PARTITION BY t.MACHINE_ID ORDER BY t.READING_TS),
        t.VIBRATION_MM_S)                                             AS VIB_DELTA_6H,
    AVG(t.PROCESS_TEMP_K - t.AIR_TEMP_K) OVER (
        PARTITION BY t.MACHINE_ID ORDER BY t.READING_TS
        ROWS BETWEEN 5 PRECEDING AND CURRENT ROW)                     AS TEMP_DIFF_AVG_6H,
    AVG(t.POWER_KW) OVER (
        PARTITION BY t.MACHINE_ID ORDER BY t.READING_TS
        ROWS BETWEEN 5 PRECEDING AND CURRENT ROW)                     AS POWER_AVG_6H,
    CASE WHEN m.MACHINE_TYPE = 'CNC_MILL'  THEN 1 ELSE 0 END          AS IS_MILL,
    CASE WHEN m.MACHINE_TYPE = 'CNC_LATHE' THEN 1 ELSE 0 END          AS IS_LATHE,
    CASE WHEN m.MACHINE_TYPE = 'PRESS'     THEN 1 ELSE 0 END          AS IS_PRESS,
    CASE WHEN m.MACHINE_TYPE = 'PACKER'    THEN 1 ELSE 0 END          AS IS_PACKER
FROM TELEMETRY t
JOIN MACHINES m ON m.MACHINE_ID = t.MACHINE_ID;

-- Label: an unplanned stop starts within the next 24 hours
CREATE OR REPLACE VIEW V_TRAINING AS
SELECT
    f.*,
    CASE WHEN EXISTS (
        SELECT 1 FROM DOWNTIME d
        WHERE d.MACHINE_ID = f.MACHINE_ID
          AND d.STOP_TYPE  = 'UNPLANNED'
          AND d.START_TS   >  f.READING_TS
          AND d.START_TS   <= ADD_HOURS(f.READING_TS, 24)
    ) THEN 1 ELSE 0 END AS LABEL_FAIL_24H
FROM V_FEATURES f;
