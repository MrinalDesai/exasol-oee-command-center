-- Step 5: SUPPLIERS REPORTING IN INCONSISTENT FORMATS.
--
-- Three supplier catalogs arrive as delivered: a CSV in rupees and days, JSON
-- documents in dollars and weeks, and a CSV with text prices and free-text
-- delivery. Exasol
--   1. reads all three into one offer list (JSON_VALUE, REGEXP_SUBSTR, FX table),
--   2. matches each offer to our part master by the manufacturer code, after
--      normalising how each supplier writes it (case, spaces, dashes, "45*62*8"),
--   3. accepts exact code matches automatically and sends near matches
--      (EDIT_DISTANCE) to a buyer for review, never into an order,
--   4. turns what the plants need into a purchase list: the BOM to order.
OPEN SCHEMA OEE;

-- 1. One offer list, in rupees and days
CREATE OR REPLACE VIEW V_SUPPLIER_OFFERS AS
SELECT 'A' AS SUPPLIER_ID, ITEM_CODE AS SUPPLIER_CODE, ITEM_DESC AS DESCRIPTION,
       PRICE_INR * 1.0 AS UNIT_PRICE_INR, LEAD_DAYS * 1.0 AS LEAD_DAYS, MOQ * 1.0 AS ORDER_MULTIPLE,
       ITEM_CODE || ',' || ITEM_DESC || ',' || CAST(PRICE_INR AS VARCHAR(20)) || ',' || CAST(LEAD_DAYS AS VARCHAR(10))
         || ',' || CAST(MOQ AS VARCHAR(10)) AS RAW_LINE
FROM SRC_SUPPLIER_A
UNION ALL
SELECT 'B',
       JSON_VALUE(b.RAW_JSON, '$.sku'),
       JSON_VALUE(b.RAW_JSON, '$.title'),
       CAST(JSON_VALUE(b.RAW_JSON, '$.price.usd') AS DOUBLE) * fx.INR_PER_UNIT,
       CAST(JSON_VALUE(b.RAW_JSON, '$.lead_time_weeks') AS DOUBLE) * 7,
       CAST(JSON_VALUE(b.RAW_JSON, '$.pack_qty') AS DOUBLE),
       b.RAW_JSON
FROM SRC_SUPPLIER_B b
JOIN FX_RATES fx ON fx.CURRENCY = 'USD'
UNION ALL
SELECT 'C', PARTNUMBER, NAME,
       CAST(REGEXP_SUBSTR(REPLACE(RATE, ',', ''), '[0-9]+(\.[0-9]+)?') AS DOUBLE),
       CAST(REGEXP_SUBSTR(DELIVERY, '[0-9]+') AS DOUBLE)
         * CASE WHEN UPPER(DELIVERY) LIKE '%W%' THEN 7 ELSE 1 END,
       1.0,
       PARTNUMBER || ',' || NAME || ',"' || RATE || '",' || DELIVERY
FROM SRC_SUPPLIER_C;

-- 2. Normalised text: the whole description squashed to A-Z/0-9 (for exact code
--    matches), and the first three words that carry a digit (for near matches)
CREATE OR REPLACE VIEW V_OFFER_TEXT AS
WITH t AS (
    SELECT o.*,
           UPPER(o.DESCRIPTION) AS UP,
           REGEXP_REPLACE(REGEXP_REPLACE(UPPER(o.DESCRIPTION), '[*]', 'X'), '[^A-Z0-9]', '') AS SQUASHED,
           REGEXP_REPLACE(UPPER(o.DESCRIPTION), '[-./]', '') AS WORDS
    FROM V_SUPPLIER_OFFERS o
)
SELECT t.*,
       REGEXP_SUBSTR(WORDS, '[A-Z]*[0-9][A-Z0-9]*', 1, 1) AS TOKEN_1,
       REGEXP_SUBSTR(WORDS, '[A-Z]*[0-9][A-Z0-9]*', 1, 2) AS TOKEN_2,
       REGEXP_SUBSTR(WORDS, '[A-Z]*[0-9][A-Z0-9]*', 1, 3) AS TOKEN_3
FROM t;

-- 3. Candidate matches: right category, and the code is in the text (EXACT) or one
--    of its coded words is within 1 edit (codes of 6-7 characters) or 2 edits
--    (8+). Short codes such as 6205 must match exactly: 6204 is a different bearing.
CREATE OR REPLACE TABLE SUPPLIER_MATCHES AS
WITH cand AS (
    SELECT t.SUPPLIER_ID, t.SUPPLIER_CODE, t.DESCRIPTION, t.UNIT_PRICE_INR, t.LEAD_DAYS, t.ORDER_MULTIPLE,
           p.PART_NO, p.MATCH_KEY,
           CASE WHEN INSTR(t.SQUASHED, p.MATCH_KEY) > 0 THEN 0
                ELSE LEAST(COALESCE(EDIT_DISTANCE(t.TOKEN_1, p.MATCH_KEY), 99),
                           COALESCE(EDIT_DISTANCE(t.TOKEN_2, p.MATCH_KEY), 99),
                           COALESCE(EDIT_DISTANCE(t.TOKEN_3, p.MATCH_KEY), 99)) END AS DIST
    FROM V_OFFER_TEXT t
    CROSS JOIN PART_MASTER p
    JOIN CATEGORY_TERMS c ON c.CATEGORY = p.CATEGORY AND INSTR(t.UP, c.TERM) > 0
),
typed AS (
    SELECT c.*,
           CASE WHEN DIST = 0 THEN 'EXACT'
                WHEN LENGTH(MATCH_KEY) >= 8 AND DIST <= 2 THEN 'REVIEW'
                WHEN LENGTH(MATCH_KEY) >= 6 AND DIST <= 1 THEN 'REVIEW'
           END AS MATCH_TYPE
    FROM cand c
),
ranked AS (
    SELECT t.*,
           ROW_NUMBER() OVER (PARTITION BY SUPPLIER_ID, SUPPLIER_CODE
                              ORDER BY DIST, LENGTH(MATCH_KEY) DESC) AS RN
    FROM typed t
    WHERE MATCH_TYPE IS NOT NULL
)
SELECT SUPPLIER_ID, SUPPLIER_CODE, DESCRIPTION, PART_NO, MATCH_TYPE, DIST AS EDIT_DIST,
       UNIT_PRICE_INR, LEAD_DAYS, ORDER_MULTIPLE
FROM ranked
WHERE RN = 1;

-- How good the matching is, against the buyer-verified mapping
CREATE OR REPLACE VIEW V_MATCH_ACCURACY AS
WITH j AS (
    SELECT a.SUPPLIER_ID, a.SUPPLIER_CODE, a.TRUE_PART_NO, m.PART_NO, m.MATCH_TYPE
    FROM SUPPLIER_MATCH_AUDIT a
    LEFT JOIN SUPPLIER_MATCHES m ON m.SUPPLIER_ID = a.SUPPLIER_ID AND m.SUPPLIER_CODE = a.SUPPLIER_CODE
)
SELECT COUNT(*)                                                                          AS CATALOG_LINES,
       SUM(CASE WHEN TRUE_PART_NO IS NOT NULL THEN 1 ELSE 0 END)                         AS LINES_FOR_OUR_PARTS,
       SUM(CASE WHEN MATCH_TYPE = 'EXACT' THEN 1 ELSE 0 END)                             AS AUTO_MATCHED,
       SUM(CASE WHEN MATCH_TYPE = 'EXACT' AND PART_NO = TRUE_PART_NO THEN 1 ELSE 0 END)  AS AUTO_CORRECT,
       SUM(CASE WHEN MATCH_TYPE = 'REVIEW' THEN 1 ELSE 0 END)                            AS SENT_TO_REVIEW,
       SUM(CASE WHEN MATCH_TYPE = 'REVIEW' AND PART_NO = TRUE_PART_NO THEN 1 ELSE 0 END) AS REVIEW_CORRECT,
       SUM(CASE WHEN TRUE_PART_NO IS NOT NULL AND MATCH_TYPE IS NULL THEN 1 ELSE 0 END)  AS MISSED,
       SUM(CASE WHEN MATCH_TYPE = 'EXACT' AND PART_NO = TRUE_PART_NO THEN 1 ELSE 0 END) * 1.0
         / NULLIF(SUM(CASE WHEN MATCH_TYPE = 'EXACT' THEN 1 ELSE 0 END), 0)              AS AUTO_PRECISION,
       SUM(CASE WHEN MATCH_TYPE = 'EXACT' AND PART_NO = TRUE_PART_NO THEN 1 ELSE 0 END) * 1.0
         / NULLIF(SUM(CASE WHEN TRUE_PART_NO IS NOT NULL THEN 1 ELSE 0 END), 0)          AS AUTO_RECALL
FROM j;

-- Every supplier line for every part, side by side (what the console shows)
CREATE OR REPLACE VIEW V_PART_OFFERS AS
SELECT m.PART_NO, m.SUPPLIER_ID, s.NAME AS SUPPLIER_NAME, s.CATALOG_FORMAT, m.SUPPLIER_CODE, m.DESCRIPTION,
       m.MATCH_TYPE, m.EDIT_DIST, m.UNIT_PRICE_INR, m.LEAD_DAYS, m.ORDER_MULTIPLE
FROM SUPPLIER_MATCHES m
JOIN SUPPLIERS s ON s.SUPPLIER_ID = m.SUPPLIER_ID;

-- 4. The BOM to order: parts at-risk machines must get from a supplier (no store
--    in the network has them), plus what brings network stock back to its reorder
--    points once the at-risk repairs have taken their kits from stores. Urgent lines
--    go to the fastest verified supplier, replenishment to the cheapest.
CREATE OR REPLACE VIEW V_BOM_TO_ORDER AS
WITH at_risk AS (
    SELECT PART_NO,
           SUM(CASE WHEN SOURCE_KIND = 'SUPPLIER' THEN QTY ELSE 0 END)  AS URGENT,
           SUM(CASE WHEN SOURCE_KIND <> 'SUPPLIER' THEN QTY ELSE 0 END) AS FROM_STORES
    FROM V_MACHINE_SOURCING
    WHERE HEALTH IN ('watch', 'critical') AND OPEN_WO_ID IS NULL
    GROUP BY PART_NO
),
network AS (
    SELECT PART_NO, SUM(REORDER_POINT) AS REORDER_TOTAL, SUM(AVAILABLE) AS AVAILABLE_TOTAL
    FROM V_PART_AVAILABLE
    GROUP BY PART_NO
),
need AS (
    SELECT p.PART_NO, p.DESCRIPTION,
           COALESCE(u.URGENT, 0)                                           AS URGENT_QTY,
           GREATEST(COALESCE(n.REORDER_TOTAL, 0)
                    - (COALESCE(n.AVAILABLE_TOTAL, 0) - COALESCE(u.FROM_STORES, 0)), 0) AS REPLENISH_QTY
    FROM PART_MASTER p
    LEFT JOIN at_risk u ON u.PART_NO = p.PART_NO
    LEFT JOIN network n ON n.PART_NO = p.PART_NO
),
pick AS (
    SELECT n.*, o.SUPPLIER_ID, o.SUPPLIER_NAME, o.SUPPLIER_CODE, o.DESCRIPTION AS SUPPLIER_DESCRIPTION,
           o.UNIT_PRICE_INR, o.LEAD_DAYS, o.ORDER_MULTIPLE,
           ROW_NUMBER() OVER (PARTITION BY n.PART_NO ORDER BY
               CASE WHEN n.URGENT_QTY > 0 THEN o.LEAD_DAYS ELSE o.UNIT_PRICE_INR END,
               CASE WHEN n.URGENT_QTY > 0 THEN o.UNIT_PRICE_INR ELSE o.LEAD_DAYS END) AS RN
    FROM need n
    LEFT JOIN V_PART_OFFERS o ON o.PART_NO = n.PART_NO AND o.MATCH_TYPE = 'EXACT'
    WHERE n.URGENT_QTY + n.REPLENISH_QTY > 0
)
SELECT PART_NO, DESCRIPTION, URGENT_QTY, REPLENISH_QTY,
       CEIL((URGENT_QTY + REPLENISH_QTY) / COALESCE(ORDER_MULTIPLE, 1)) * COALESCE(ORDER_MULTIPLE, 1) AS ORDER_QTY,
       SUPPLIER_ID, SUPPLIER_NAME, SUPPLIER_CODE, SUPPLIER_DESCRIPTION, UNIT_PRICE_INR, LEAD_DAYS,
       CEIL((URGENT_QTY + REPLENISH_QTY) / COALESCE(ORDER_MULTIPLE, 1)) * COALESCE(ORDER_MULTIPLE, 1)
         * UNIT_PRICE_INR                                                AS LINE_TOTAL_INR,
       CASE WHEN SUPPLIER_ID IS NULL THEN 'NO VERIFIED SUPPLIER'
            WHEN URGENT_QTY > 0 THEN 'URGENT' ELSE 'REPLENISH' END       AS PRIORITY
FROM pick
WHERE RN = 1;
