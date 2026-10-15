-- In-database scoring with the compiled FAILURE_RISK UDF, plus decision views.
-- {FEATURE_COLUMNS} is filled in by `oee run-sql` from oee/features.py:FEATURES
-- so the UDF argument order can never drift from the model.
OPEN SCHEMA OEE;

-- Score every reading once and materialise (fast dashboards, stable demo)
CREATE OR REPLACE TABLE RISK_SCORES AS
SELECT
    MACHINE_ID, PLANT_ID, MACHINE_TYPE, READING_TS,
    TOOL_WEAR_MIN, VIBRATION_MM_S, TEMP_DIFF_K, POWER_KW, TORQUE_RATIO,
    FAILURE_RISK({FEATURE_COLUMNS}) AS RISK_24H
FROM V_FEATURES;

-- Latest risk per machine, with reliability context
CREATE OR REPLACE VIEW V_RISK_LATEST AS
SELECT r.*, rel.MTBF_HOURS, rel.MTTR_HOURS, rel.FAILURES
FROM RISK_SCORES r
JOIN (SELECT MACHINE_ID, MAX(READING_TS) AS TS FROM RISK_SCORES GROUP BY MACHINE_ID) lt
  ON lt.MACHINE_ID = r.MACHINE_ID AND lt.TS = r.READING_TS
JOIN V_RELIABILITY rel ON rel.MACHINE_ID = r.MACHINE_ID;

-- Historical mix of failure modes (and the part each consumes) per machine type
CREATE OR REPLACE VIEW V_MODE_MIX AS
SELECT
    m.MACHINE_TYPE,
    d.PART_REPLACED                                           AS PART_NO,
    COUNT(*) * 1.0 / SUM(COUNT(*)) OVER (PARTITION BY m.MACHINE_TYPE) AS SHARE
FROM DOWNTIME d
JOIN MACHINES m ON m.MACHINE_ID = d.MACHINE_ID
WHERE d.STOP_TYPE = 'UNPLANNED'
GROUP BY m.MACHINE_TYPE, d.PART_REPLACED;

-- Spare-parts exposure: expected part demand from current risk vs stock on hand, per plant.
-- Answers "are the parts we are about to need in the right place?"
CREATE OR REPLACE VIEW V_PARTS_EXPOSURE AS
WITH part_demand AS (
    SELECT r.PLANT_ID, mm.PART_NO, SUM(r.RISK_24H * mm.SHARE) AS EXPECTED_DEMAND_24H
    FROM V_RISK_LATEST r
    JOIN V_MODE_MIX mm ON mm.MACHINE_TYPE = r.MACHINE_TYPE
    GROUP BY r.PLANT_ID, mm.PART_NO
)
SELECT
    s.PLANT_ID, s.PART_NO, s.ON_HAND, s.REORDER_POINT, s.LEAD_TIME_DAYS, s.UNIT_COST_INR,
    COALESCE(d.EXPECTED_DEMAND_24H, 0)                         AS EXPECTED_DEMAND_24H,
    CASE
        WHEN s.ON_HAND = 0 AND COALESCE(d.EXPECTED_DEMAND_24H, 0) >= 0.3 THEN 'STOCKOUT_RISK'
        WHEN s.ON_HAND < s.REORDER_POINT AND COALESCE(d.EXPECTED_DEMAND_24H, 0) >= 0.1 THEN 'LOW'
        ELSE 'OK'
    END                                                       AS STATUS
FROM SPARE_PARTS s
LEFT JOIN part_demand d ON d.PLANT_ID = s.PLANT_ID AND d.PART_NO = s.PART_NO;
