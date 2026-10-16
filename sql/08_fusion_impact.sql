-- What combining and completing the data is worth.
--
-- The same model (OEE.FAILURE_RISK) scores two versions of the plant's history:
--   1. the raw sources joined as delivered (TELEMETRY_NAIVE, see 02_fusion.sql)
--   2. the data Exasol combined and completed (TELEMETRY)
-- and we count how many real unplanned stops each version warned about in the
-- 24 hours before they happened. Only the held-out period after the model's
-- training cutoff counts, so neither version is scored on data it learned from. V_FEATURES_NAIVE is created from the same
-- feature definitions as V_FEATURES (oee/pipeline.py renders it).
OPEN SCHEMA OEE;

CREATE OR REPLACE TABLE RISK_SCORES_NAIVE AS
SELECT MACHINE_ID, READING_TS, FAILURE_RISK({FEATURE_COLUMNS}) AS RISK_24H
FROM V_FEATURES_NAIVE;

CREATE OR REPLACE TABLE FUSION_IMPACT AS
WITH cutoff AS (
    SELECT MAX(TRAIN_CUTOFF) AS TS FROM MODEL_REGISTRY WHERE MODEL_NAME = 'FAILURE_RISK'
),
scored AS (
    SELECT x.* FROM (
        SELECT 1 AS ORD, 'Raw sources, joined as delivered' AS PIPELINE, MACHINE_ID, READING_TS, RISK_24H FROM RISK_SCORES_NAIVE
        UNION ALL
        SELECT 2, 'Combined and completed in Exasol', MACHINE_ID, READING_TS, RISK_24H FROM RISK_SCORES
    ) x
    CROSS JOIN cutoff c
    WHERE x.READING_TS > c.TS
),
pipelines AS (
    SELECT DISTINCT ORD, PIPELINE FROM scored
),
stops AS (
    SELECT d.STOP_ID, d.MACHINE_ID, d.START_TS
    FROM DOWNTIME d CROSS JOIN cutoff c
    WHERE d.STOP_TYPE = 'UNPLANNED' AND HOURS_BETWEEN(d.START_TS, c.TS) > 24   -- a full 24 h warning window after the cutoff
),
per_stop AS (
    SELECT p.ORD, p.PIPELINE, st.STOP_ID, st.START_TS,
           MIN(CASE WHEN s.RISK_24H >= 0.5 THEN s.READING_TS END) AS FIRST_ALERT,
           COUNT(s.READING_TS)                                     AS HOURS_SEEN
    FROM stops st
    CROSS JOIN pipelines p
    LEFT JOIN scored s
      ON  s.ORD = p.ORD
      AND s.MACHINE_ID = st.MACHINE_ID
      AND s.READING_TS < st.START_TS
      AND HOURS_BETWEEN(st.START_TS, s.READING_TS) <= 24
    GROUP BY p.ORD, p.PIPELINE, st.STOP_ID, st.START_TS
),
alert_hits AS (
    SELECT DISTINCT s.ORD, s.MACHINE_ID, s.READING_TS
    FROM scored s
    JOIN stops st
      ON  st.MACHINE_ID = s.MACHINE_ID
      AND st.START_TS > s.READING_TS
      AND HOURS_BETWEEN(st.START_TS, s.READING_TS) <= 24
    WHERE s.RISK_24H >= 0.5
),
alerts AS (
    SELECT s.ORD,
           COUNT(*)                                   AS HOURS_SCORED,
           SUM(CASE WHEN s.RISK_24H >= 0.5 THEN 1 ELSE 0 END) AS ALERT_HOURS
    FROM scored s
    GROUP BY s.ORD
),
hits AS (
    SELECT ORD, COUNT(*) AS ALERTS_FOLLOWED FROM alert_hits GROUP BY ORD
)
SELECT ps.ORD,
       ps.PIPELINE,
       a.HOURS_SCORED,
       COUNT(*)                                                        AS STOPS,
       SUM(CASE WHEN ps.FIRST_ALERT IS NOT NULL THEN 1 ELSE 0 END)     AS STOPS_WARNED,
       SUM(CASE WHEN ps.FIRST_ALERT IS NOT NULL THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS CATCH_RATE,
       AVG(CASE WHEN ps.FIRST_ALERT IS NOT NULL THEN HOURS_BETWEEN(ps.START_TS, ps.FIRST_ALERT) END) AS AVG_WARNING_HOURS,
       a.ALERT_HOURS,
       COALESCE(h.ALERTS_FOLLOWED, 0) * 1.0 / NULLIF(a.ALERT_HOURS, 0) AS ALERT_PRECISION
FROM per_stop ps
JOIN alerts a ON a.ORD = ps.ORD
LEFT JOIN hits h ON h.ORD = ps.ORD
GROUP BY ps.ORD, ps.PIPELINE, a.HOURS_SCORED, a.ALERT_HOURS, h.ALERTS_FOLLOWED;
