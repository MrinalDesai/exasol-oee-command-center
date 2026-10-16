-- Step 3: SPARE PARTS HELD IN THE WRONG LOCATION.
--
-- For the repair each machine is heading for (its root cause, 06/07), Exasol
-- looks up the repair kit, checks the machine's own plant store, and if a part
-- is missing there scans every other store in the network, compares the road
-- transfer time with the supplier's lead time, and picks the fastest source.
-- Stock already reserved for open work orders is not offered twice.
OPEN SCHEMA OEE;

-- Free stock per store: on hand minus what open work orders have reserved from it
CREATE OR REPLACE VIEW V_PART_AVAILABLE AS
WITH reserved AS (
    SELECT FROM_LOCATION, PART_NO, SUM(QTY) AS QTY
    FROM PART_RESERVATIONS
    WHERE STATUS = 'RESERVED' AND FROM_LOCATION IS NOT NULL
    GROUP BY FROM_LOCATION, PART_NO
)
SELECT s.PLANT_ID AS LOCATION_ID, s.PART_NO, s.ON_HAND,
       COALESCE(r.QTY, 0)              AS RESERVED_QTY,
       s.ON_HAND - COALESCE(r.QTY, 0)  AS AVAILABLE,
       s.REORDER_POINT, s.LEAD_TIME_DAYS, s.UNIT_COST_INR
FROM SPARE_PARTS s
LEFT JOIN reserved r ON r.FROM_LOCATION = s.PLANT_ID AND r.PART_NO = s.PART_NO;

-- What each plant needs for each kind of repair, and every way to get each part there
CREATE OR REPLACE VIEW V_SOURCING_OPTIONS AS
WITH need AS (
    SELECT p.LOCATION_ID AS PLANT_ID, k.FAILURE_MODE, k.PART_NO, k.QTY
    FROM REPAIR_KITS k
    CROSS JOIN (SELECT LOCATION_ID FROM LOCATIONS WHERE KIND = 'PLANT') p
),
local_stock AS (
    SELECT n.PLANT_ID, n.FAILURE_MODE, n.PART_NO, n.QTY,
           'LOCAL' AS SOURCE_KIND, n.PLANT_ID AS FROM_LOCATION,
           0.0 AS ETA_HOURS, 0.0 AS COST_INR, a.AVAILABLE
    FROM need n
    JOIN V_PART_AVAILABLE a ON a.LOCATION_ID = n.PLANT_ID AND a.PART_NO = n.PART_NO
    WHERE a.AVAILABLE >= n.QTY
),
transfer AS (
    SELECT n.PLANT_ID, n.FAILURE_MODE, n.PART_NO, n.QTY,
           'TRANSFER' AS SOURCE_KIND, a.LOCATION_ID AS FROM_LOCATION,
           l.TRANSFER_HOURS * 1.0 AS ETA_HOURS, l.COST_INR * 1.0 AS COST_INR, a.AVAILABLE
    FROM need n
    JOIN V_PART_AVAILABLE a ON a.PART_NO = n.PART_NO AND a.LOCATION_ID <> n.PLANT_ID
    JOIN TRANSFER_LANES l   ON l.FROM_LOCATION = a.LOCATION_ID AND l.TO_LOCATION = n.PLANT_ID
    WHERE a.AVAILABLE >= n.QTY
),
supplier AS (
    SELECT n.PLANT_ID, n.FAILURE_MODE, n.PART_NO, n.QTY,
           'SUPPLIER' AS SOURCE_KIND, CAST(NULL AS VARCHAR(8)) AS FROM_LOCATION,
           a.LEAD_TIME_DAYS * 24.0 AS ETA_HOURS, a.UNIT_COST_INR * n.QTY * 1.0 AS COST_INR,
           CAST(NULL AS DECIMAL(8,0)) AS AVAILABLE
    FROM need n
    JOIN V_PART_AVAILABLE a ON a.LOCATION_ID = n.PLANT_ID AND a.PART_NO = n.PART_NO
),
opts AS (
    SELECT * FROM local_stock
    UNION ALL SELECT * FROM transfer
    UNION ALL SELECT * FROM supplier
)
SELECT o.*,
       ROW_NUMBER() OVER (PARTITION BY PLANT_ID, FAILURE_MODE, PART_NO ORDER BY ETA_HOURS, COST_INR) AS OPTION_RANK
FROM opts o;

-- The fastest source per part, with what waiting for the supplier would have cost
CREATE OR REPLACE VIEW V_SOURCING_BEST AS
WITH sup AS (
    SELECT PLANT_ID, FAILURE_MODE, PART_NO, ETA_HOURS AS SUPPLIER_HOURS
    FROM V_SOURCING_OPTIONS WHERE SOURCE_KIND = 'SUPPLIER'
),
loc AS (
    SELECT PLANT_ID, FAILURE_MODE, PART_NO, 1 AS HAS_LOCAL
    FROM V_SOURCING_OPTIONS WHERE SOURCE_KIND = 'LOCAL'
)
SELECT o.PLANT_ID, o.FAILURE_MODE, o.PART_NO, o.QTY, o.SOURCE_KIND, o.FROM_LOCATION, o.ETA_HOURS, o.COST_INR,
       s.SUPPLIER_HOURS,
       CASE WHEN l.HAS_LOCAL = 1 THEN 0.0 ELSE s.SUPPLIER_HOURS END   AS LOCAL_ONLY_HOURS
FROM V_SOURCING_OPTIONS o
JOIN sup s       ON s.PLANT_ID = o.PLANT_ID AND s.FAILURE_MODE = o.FAILURE_MODE AND s.PART_NO = o.PART_NO
LEFT JOIN loc l  ON l.PLANT_ID = o.PLANT_ID AND l.FAILURE_MODE = o.FAILURE_MODE AND l.PART_NO = o.PART_NO
WHERE o.OPTION_RANK = 1;

-- Per plant and repair: when the whole kit can be on site, scanning the network
-- versus only looking at the plant's own store and otherwise ordering
CREATE OR REPLACE VIEW V_KIT_COVERAGE AS
SELECT PLANT_ID, FAILURE_MODE,
       COUNT(*)                                                    AS KIT_PARTS,
       SUM(CASE WHEN SOURCE_KIND = 'LOCAL' THEN 1 ELSE 0 END)      AS PARTS_LOCAL,
       SUM(CASE WHEN SOURCE_KIND = 'TRANSFER' THEN 1 ELSE 0 END)   AS PARTS_TRANSFER,
       SUM(CASE WHEN SOURCE_KIND = 'SUPPLIER' THEN 1 ELSE 0 END)   AS PARTS_SUPPLIER,
       MAX(ETA_HOURS)                                              AS KIT_READY_HOURS,
       MAX(LOCAL_ONLY_HOURS)                                       AS LOCAL_ONLY_HOURS,
       MAX(LOCAL_ONLY_HOURS) - MAX(ETA_HOURS)                      AS HOURS_SAVED
FROM V_SOURCING_BEST
GROUP BY PLANT_ID, FAILURE_MODE;

-- The sourcing plan for each machine's most likely repair
CREATE OR REPLACE VIEW V_MACHINE_SOURCING AS
SELECT s.MACHINE_ID, s.PLANT_ID, s.HEALTH, s.RISK_24H, s.TOP_MODE, s.OPEN_WO_ID,
       b.PART_NO, b.QTY, b.SOURCE_KIND, b.FROM_LOCATION, b.ETA_HOURS, b.COST_INR,
       b.SUPPLIER_HOURS, b.LOCAL_ONLY_HOURS
FROM V_MACHINE_STATUS s
JOIN V_SOURCING_BEST b ON b.PLANT_ID = s.PLANT_ID AND b.FAILURE_MODE = s.TOP_MODE;

-- Fleet view for machines that need attention now (watch or critical, no work order yet)
CREATE OR REPLACE VIEW V_SOURCING_IMPACT AS
WITH per_machine AS (
    SELECT MACHINE_ID,
           MAX(ETA_HOURS)        AS KIT_READY_HOURS,
           MAX(LOCAL_ONLY_HOURS) AS LOCAL_ONLY_HOURS,
           MIN(CASE WHEN SOURCE_KIND = 'LOCAL' THEN 1 ELSE 0 END) AS KIT_LOCAL
    FROM V_MACHINE_SOURCING
    WHERE HEALTH IN ('watch', 'critical') AND OPEN_WO_ID IS NULL
    GROUP BY MACHINE_ID
)
SELECT COUNT(*)                                       AS MACHINES_AT_RISK,
       SUM(KIT_LOCAL)                                 AS KITS_ON_SITE,
       COUNT(*) - SUM(KIT_LOCAL)                      AS KITS_INCOMPLETE_ON_SITE,
       SUM(LOCAL_ONLY_HOURS)                          AS WAIT_HOURS_LOCAL_ONLY,
       SUM(KIT_READY_HOURS)                           AS WAIT_HOURS_NETWORK,
       SUM(LOCAL_ONLY_HOURS) - SUM(KIT_READY_HOURS)   AS WAIT_HOURS_SAVED
FROM per_machine;
