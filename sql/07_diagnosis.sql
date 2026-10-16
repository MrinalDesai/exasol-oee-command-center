-- Per-machine diagnosis, computed entirely in Exasol.
-- Runs after 05_scoring.sql (RISK_SCORES) and 06_root_cause.sql (ROOT_CAUSE). The console only formats these results.
OPEN SCHEMA OEE;

-- Reference: failure modes and the spare part each one consumes
CREATE OR REPLACE TABLE FAILURE_MODES (
    FAILURE_MODE    VARCHAR(32) NOT NULL,
    PART_NO         VARCHAR(32) NOT NULL,
    DESCRIPTION     VARCHAR(200)
);
INSERT INTO FAILURE_MODES VALUES
    ('TOOL_WEAR',        'TOOL-INSERT-T12',   'Cutting tool worn past its limit'),
    ('HEAT_DISSIPATION', 'COOLANT-PUMP-CP4',  'Process heat not carried away by the coolant'),
    ('POWER',            'SPINDLE-DRIVE-SD2', 'Drive power outside its operating window'),
    ('OVERSTRAIN',       'GEARBOX-SEAL-GS7',  'High torque on worn tooling overloads the drivetrain'),
    ('BEARING',          'BEARING-6205',      'Bearing wear, seen as rising vibration');

-- Latest reading per machine, each signal compared with its machine type's normal
CREATE OR REPLACE VIEW V_SIGNALS AS
WITH base AS (
    SELECT MACHINE_TYPE,
        AVG(TOOL_WEAR_MIN)  AS TW_BASE,  STDDEV(TOOL_WEAR_MIN)  AS TW_SD,
        AVG(VIBRATION_MM_S) AS VIB_BASE, STDDEV(VIBRATION_MM_S) AS VIB_SD,
        AVG(TEMP_DIFF_K)    AS TD_BASE,  STDDEV(TEMP_DIFF_K)    AS TD_SD,
        AVG(POWER_KW)       AS PW_BASE,  STDDEV(POWER_KW)       AS PW_SD,
        AVG(TORQUE_RATIO)   AS TR_BASE,  STDDEV(TORQUE_RATIO)   AS TR_SD
    FROM RISK_SCORES
    GROUP BY MACHINE_TYPE
)
SELECT r.MACHINE_ID, r.PLANT_ID, m.LINE_ID, r.MACHINE_TYPE, r.READING_TS, r.RISK_24H,
       r.MTBF_HOURS, r.MTTR_HOURS, r.FAILURES,
       r.TOOL_WEAR_MIN, r.VIBRATION_MM_S, r.TEMP_DIFF_K, r.POWER_KW, r.TORQUE_RATIO,
       b.TW_BASE, b.VIB_BASE, b.TD_BASE, b.PW_BASE, b.TR_BASE,
       (r.TOOL_WEAR_MIN  - b.TW_BASE)  / NULLIF(b.TW_SD, 0)  AS TW_Z,
       (r.VIBRATION_MM_S - b.VIB_BASE) / NULLIF(b.VIB_SD, 0) AS VIB_Z,
       (r.TEMP_DIFF_K    - b.TD_BASE)  / NULLIF(b.TD_SD, 0)  AS TD_Z,
       (r.POWER_KW       - b.PW_BASE)  / NULLIF(b.PW_SD, 0)  AS PW_Z,
       (r.TORQUE_RATIO   - b.TR_BASE)  / NULLIF(b.TR_SD, 0)  AS TR_Z
FROM V_RISK_LATEST r
JOIN MACHINES m ON m.MACHINE_ID = r.MACHINE_ID
JOIN base b ON b.MACHINE_TYPE = r.MACHINE_TYPE;

-- The same signals, one row each, ranked by how far they sit from normal
CREATE OR REPLACE VIEW V_SIGNAL_DEVIATIONS AS
WITH u AS (
    SELECT MACHINE_ID, 'TOOL_WEAR_MIN'  AS SIGNAL_NAME, TOOL_WEAR_MIN  AS SIGNAL_VALUE, TW_BASE  AS BASELINE, TW_Z  AS Z FROM V_SIGNALS
    UNION ALL
    SELECT MACHINE_ID, 'VIBRATION_MM_S', VIBRATION_MM_S, VIB_BASE, VIB_Z FROM V_SIGNALS
    UNION ALL
    SELECT MACHINE_ID, 'TEMP_DIFF_K',    TEMP_DIFF_K,    TD_BASE,  TD_Z  FROM V_SIGNALS
    UNION ALL
    SELECT MACHINE_ID, 'POWER_KW',       POWER_KW,       PW_BASE,  PW_Z  FROM V_SIGNALS
    UNION ALL
    SELECT MACHINE_ID, 'TORQUE_RATIO',   TORQUE_RATIO,   TR_BASE,  TR_Z  FROM V_SIGNALS
)
SELECT MACHINE_ID, SIGNAL_NAME, SIGNAL_VALUE, BASELINE, Z,
       ROW_NUMBER() OVER (PARTITION BY MACHINE_ID ORDER BY ABS(COALESCE(Z, 0)) DESC) AS DEV_RANK
FROM u;

-- How often each machine type has failed in each mode, and how long those stops took
CREATE OR REPLACE VIEW V_MODE_HISTORY AS
SELECT m.MACHINE_TYPE, d.FAILURE_MODE,
       COUNT(*)                                            AS STOPS,
       AVG(SECONDS_BETWEEN(d.END_TS, d.START_TS) / 3600.0) AS AVG_HOURS
FROM DOWNTIME d
JOIN MACHINES m ON m.MACHINE_ID = d.MACHINE_ID
WHERE d.STOP_TYPE = 'UNPLANNED'
GROUP BY m.MACHINE_TYPE, d.FAILURE_MODE;

-- Likely failure mode for each machine right now: the root cause from
-- 06_root_cause.sql (stream patterns matched to learned fault signatures),
-- with the part each mode consumes and the type's repair history.
CREATE OR REPLACE VIEW V_MODE_LIKELIHOOD AS
SELECT r.MACHINE_ID, m.MACHINE_TYPE, r.FAILURE_MODE, f.PART_NO,
       SQRT(r.D2)                AS SIGNATURE_DISTANCE,
       COALESCE(h.STOPS, 0)      AS HISTORY_STOPS,
       h.AVG_HOURS,
       r.PRIOR_SHARE,
       r.CONFIDENCE              AS PROBABILITY,
       r.CAUSE_RANK              AS MODE_RANK
FROM V_ROOT_CAUSE_LATEST r
JOIN MACHINES m       ON m.MACHINE_ID = r.MACHINE_ID
JOIN FAILURE_MODES f  ON f.FAILURE_MODE = r.FAILURE_MODE
LEFT JOIN V_MODE_HISTORY h ON h.MACHINE_TYPE = m.MACHINE_TYPE AND h.FAILURE_MODE = r.FAILURE_MODE;

-- One row per machine: status, likely mode, the part it needs, stock at its plant,
-- open work order, pipeline stage and expected unplanned downtime in the next 24 h
CREATE OR REPLACE VIEW V_MACHINE_STATUS AS
WITH tm AS (
    SELECT * FROM V_MODE_LIKELIHOOD WHERE MODE_RANK = 1
),
fleet_mode AS (
    SELECT FAILURE_MODE, AVG(SECONDS_BETWEEN(END_TS, START_TS) / 3600.0) AS FLEET_AVG_HOURS
    FROM DOWNTIME WHERE STOP_TYPE = 'UNPLANNED' GROUP BY FAILURE_MODE
),
planned AS (
    SELECT AVG(SECONDS_BETWEEN(END_TS, START_TS) / 3600.0) AS PLANNED_HOURS
    FROM DOWNTIME WHERE STOP_TYPE = 'PLANNED'
),
open_wo AS (
    SELECT MACHINE_ID, MAX(WO_ID) AS WO_ID, MAX(FAILURE_MODE) AS WO_MODE
    FROM WORK_ORDERS WHERE STATUS = 'OPEN' GROUP BY MACHINE_ID
)
SELECT s.MACHINE_ID, s.PLANT_ID, s.LINE_ID, s.MACHINE_TYPE, s.READING_TS, s.RISK_24H,
       s.MTBF_HOURS, s.MTTR_HOURS, s.FAILURES,
       CASE WHEN s.RISK_24H >= 0.5  THEN 'critical'
            WHEN s.RISK_24H >= 0.10 THEN 'watch'
            ELSE 'healthy' END                                                AS HEALTH,
       tm.FAILURE_MODE                                                        AS TOP_MODE,
       tm.PROBABILITY                                                         AS TOP_MODE_P,
       tm.PART_NO,
       sp.ON_HAND,
       sp.LEAD_TIME_DAYS,
       COALESCE(tm.AVG_HOURS, fm.FLEET_AVG_HOURS)                            AS UNPLANNED_HOURS,
       p.PLANNED_HOURS,
       w.WO_ID                                                                AS OPEN_WO_ID,
       w.WO_MODE                                                              AS OPEN_WO_MODE,
       CASE WHEN w.WO_ID IS NOT NULL THEN 'WORK_ORDER'
            WHEN sp.ON_HAND <= 0     THEN 'PARTS_BLOCKED'
            WHEN s.RISK_24H >= 0.10  THEN 'READY'
            ELSE 'MONITOR' END                                                AS STAGE,
       s.RISK_24H * COALESCE(tm.AVG_HOURS, fm.FLEET_AVG_HOURS, s.MTTR_HOURS) AS EXPECTED_DOWN_HOURS_24H
FROM V_SIGNALS s
JOIN tm               ON tm.MACHINE_ID = s.MACHINE_ID
LEFT JOIN fleet_mode fm ON fm.FAILURE_MODE = tm.FAILURE_MODE
LEFT JOIN SPARE_PARTS sp ON sp.PLANT_ID = s.PLANT_ID AND sp.PART_NO = tm.PART_NO
LEFT JOIN open_wo w   ON w.MACHINE_ID = s.MACHINE_ID
CROSS JOIN planned p;
