-- Step 4: QUALITY ISSUES CAUGHT AT THE END OF THE LINE -> caught before the shift.
--
-- OEE.QUALITY_RISK (an XGBoost model compiled into a UDF) scores every shift from
-- the machine's condition when the shift starts: HOLD >= 0.5, CHECK >= 0.3.
-- A flagged shift can get a tool
-- change or a check before it makes parts, instead of the scrap being found at
-- end-of-line inspection eight hours later.
OPEN SCHEMA OEE;

CREATE OR REPLACE TABLE QUALITY_SCORES AS
SELECT MACHINE_ID, SHIFT_START, SHIFT_END, TOTAL_COUNT, GOOD_COUNT, SCRAP_RATE, LABEL_BAD_SHIFT,
       TOOL_WEAR_MIN, VIB_AVG_6H, TEMP_DIFF_AVG_6H, PREV_SCRAP_RATE,
       QUALITY_RISK({QUALITY_COLUMNS}) AS QUALITY_RISK
FROM V_QUALITY_TRAINING;

-- Held-out shifts only (after the quality model's training cutoff). Compared with
-- what end-of-line inspection already gives a supervisor: "last shift was bad, so
-- check this one". Once a problem is running both catch it; the difference is the
-- NEW problems, the first bad shift, which only the model can see coming.
CREATE OR REPLACE TABLE QUALITY_IMPACT AS
WITH cutoff AS (SELECT MAX(TRAIN_CUTOFF) AS TS FROM MODEL_REGISTRY WHERE MODEL_NAME = 'QUALITY_RISK'),
held AS (
    SELECT q.*,
           q.TOTAL_COUNT - q.GOOD_COUNT                                       AS SCRAP_PARTS,
           GREATEST(q.TOTAL_COUNT - q.GOOD_COUNT - 0.025 * q.TOTAL_COUNT, 0) AS EXCESS_SCRAP,
           -- a new quality problem: this shift is bad but the one before it was fine,
           -- so end-of-line inspection had nothing to warn about yet
           CASE WHEN q.LABEL_BAD_SHIFT = 1 AND q.PREV_SCRAP_RATE <= 0.07 THEN 1 ELSE 0 END AS ONSET
    FROM QUALITY_SCORES q CROSS JOIN cutoff c
    WHERE q.SHIFT_START >= c.TS
),
flags AS (
    SELECT 1 AS ORD, 'QUALITY_RISK UDF in Exasol' AS METHOD_NAME, h.*,
           CASE WHEN QUALITY_RISK >= 0.3 THEN 1 ELSE 0 END AS FLAGGED   -- CHECK or HOLD
    FROM held h
    UNION ALL
    SELECT 2, 'Rule: previous shift was bad', h.*,
           CASE WHEN PREV_SCRAP_RATE > 0.07 THEN 1 ELSE 0 END
    FROM held h
)
SELECT ORD, METHOD_NAME,
       COUNT(*)                                                                AS SHIFTS,
       SUM(LABEL_BAD_SHIFT)                                                    AS BAD_SHIFTS,
       SUM(FLAGGED)                                                            AS FLAGGED_SHIFTS,
       SUM(FLAGGED * LABEL_BAD_SHIFT)                                          AS BAD_SHIFTS_FLAGGED,
       SUM(FLAGGED * LABEL_BAD_SHIFT) * 1.0 / NULLIF(SUM(LABEL_BAD_SHIFT), 0)  AS CATCH_RATE,
       SUM(FLAGGED * LABEL_BAD_SHIFT) * 1.0 / NULLIF(SUM(FLAGGED), 0)          AS FLAG_PRECISION,
       SUM(ONSET)                                                              AS NEW_PROBLEMS,
       SUM(FLAGGED * ONSET)                                                    AS NEW_PROBLEMS_FLAGGED,
       SUM(FLAGGED * ONSET) * 1.0 / NULLIF(SUM(ONSET), 0)                      AS NEW_PROBLEM_CATCH_RATE,
       AVG(LABEL_BAD_SHIFT * 1.0)                                              AS BASE_RATE,
       SUM(LABEL_BAD_SHIFT * SCRAP_PARTS)                                      AS SCRAP_IN_BAD_SHIFTS,
       SUM(FLAGGED * LABEL_BAD_SHIFT * EXCESS_SCRAP)                           AS EXCESS_SCRAP_FLAGGED_EARLY,
       SUM(LABEL_BAD_SHIFT * EXCESS_SCRAP)                                     AS EXCESS_SCRAP_ALL_BAD
FROM flags
GROUP BY ORD, METHOD_NAME;

-- The coming shift for every machine, scored from its latest reading
CREATE OR REPLACE VIEW V_QUALITY_NEXT_SHIFT AS
WITH last_shift AS (
    SELECT MACHINE_ID, SHIFT_END AS NEXT_SHIFT_START,
           (TOTAL_COUNT - GOOD_COUNT) * 1.0 / NULLIF(TOTAL_COUNT, 0) AS LAST_SCRAP_RATE,
           ROW_NUMBER() OVER (PARTITION BY MACHINE_ID ORDER BY SHIFT_START DESC) AS RN
    FROM SHIFT_PRODUCTION
),
latest AS (
    SELECT f.*, ROW_NUMBER() OVER (PARTITION BY f.MACHINE_ID ORDER BY f.READING_TS DESC) AS RN
    FROM V_FEATURES f
),
x AS (
    SELECT l.MACHINE_ID, l.PLANT_ID, l.MACHINE_TYPE, l.READING_TS, s.NEXT_SHIFT_START,
           l.TOOL_WEAR_MIN, l.VIB_AVG_6H, l.TEMP_DIFF_AVG_6H, l.TORQUE_RATIO, l.POWER_AVG_6H,
           COALESCE(s.LAST_SCRAP_RATE, 0.0) AS PREV_SCRAP_RATE,
           l.IS_MILL, l.IS_LATHE, l.IS_PRESS, l.IS_PACKER
    FROM latest l
    LEFT JOIN last_shift s ON s.MACHINE_ID = l.MACHINE_ID AND s.RN = 1
    WHERE l.RN = 1
)
SELECT MACHINE_ID, PLANT_ID, MACHINE_TYPE, READING_TS, NEXT_SHIFT_START,
       TOOL_WEAR_MIN, VIB_AVG_6H, TEMP_DIFF_AVG_6H, PREV_SCRAP_RATE,
       QUALITY_RISK({QUALITY_COLUMNS}) AS QUALITY_RISK,
       CASE WHEN QUALITY_RISK({QUALITY_COLUMNS}) >= 0.5 THEN 'HOLD'
            WHEN QUALITY_RISK({QUALITY_COLUMNS}) >= 0.3 THEN 'CHECK'
            ELSE 'RUN' END AS RECOMMENDATION
FROM x;
