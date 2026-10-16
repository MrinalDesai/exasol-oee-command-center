-- OEE Command Center: core schema on Exasol Personal
CREATE SCHEMA IF NOT EXISTS OEE;
OPEN SCHEMA OEE;

CREATE OR REPLACE TABLE MACHINES (
    MACHINE_ID      VARCHAR(32) NOT NULL,
    PLANT_ID        VARCHAR(8)  NOT NULL,
    LINE_ID         VARCHAR(16) NOT NULL,
    MACHINE_TYPE    VARCHAR(16) NOT NULL,
    INSTALL_YEAR    DECIMAL(4,0),
    IDEAL_CYCLE_S   DOUBLE,
    NOMINAL_RPM     DOUBLE,
    NOMINAL_TORQUE  DOUBLE,
    CONSTRAINT PK_MACHINES PRIMARY KEY (MACHINE_ID)
);

-- ---------------------------------------------------------------------------
-- Raw sensor data, exactly as five separate source systems deliver it.
-- Exasol combines and completes these into TELEMETRY in 02_fusion.sql.
-- ---------------------------------------------------------------------------

-- Which identifier each system uses for each machine
CREATE OR REPLACE TABLE ASSET_REGISTRY (
    MACHINE_ID      VARCHAR(32) NOT NULL,
    ASSET_TAG       VARCHAR(32),     -- production system (MES)
    CM_SENSOR_ID    VARCHAR(32),     -- condition monitor
    PLC_TAG         VARCHAR(48),     -- PLC historian
    DRIVE_ID        VARCHAR(32),     -- drive controller
    METER_ID        VARCHAR(32),     -- energy meter
    UTC_OFFSET_MIN  DECIMAL(5,0)     -- local clock of the PLC and MES at this site
);

-- Condition monitor: vibration, one reading a minute, UTC, with dropouts
CREATE OR REPLACE TABLE SRC_CM_VIBRATION (
    SENSOR_ID   VARCHAR(32),
    TS_UTC      TIMESTAMP,
    VIB_RMS     DOUBLE
);

-- PLC historian: temperatures every 5 minutes, LOCAL time, degrees Celsius
CREATE OR REPLACE TABLE SRC_PLC_TEMPS (
    PLC_TAG     VARCHAR(48),
    TS_LOCAL    TIMESTAMP,
    AIR_C       DOUBLE,
    PROCESS_C   DOUBLE
);

-- Drive controller: speed and torque, one reading a minute, UTC, with gaps
CREATE OR REPLACE TABLE SRC_DRIVE (
    DRIVE_ID    VARCHAR(32),
    TS_UTC      TIMESTAMP,
    RPM         DOUBLE,
    TORQUE_NM   DOUBLE
);

-- Energy meter: kWh per 15-minute interval, delivered hours later
CREATE OR REPLACE TABLE SRC_ENERGY (
    METER_ID            VARCHAR(32),
    INTERVAL_START_UTC  TIMESTAMP,
    KWH                 DOUBLE,
    RECEIVED_AT_UTC     TIMESTAMP
);

-- Production system: tool-wear counter, logged every 5-25 minutes, LOCAL time
CREATE OR REPLACE TABLE SRC_MES_TOOL (
    ASSET_TAG       VARCHAR(32),
    EVENT_TS_LOCAL  TIMESTAMP,
    TOOL_WEAR_MIN   DOUBLE
);

-- When the data was pulled; anything received later is not visible yet
CREATE OR REPLACE TABLE FUSION_CONFIG (
    DATA_AS_OF  TIMESTAMP
);

CREATE OR REPLACE TABLE DOWNTIME (
    STOP_ID         DECIMAL(12,0) NOT NULL,
    MACHINE_ID      VARCHAR(32)   NOT NULL,
    START_TS        TIMESTAMP     NOT NULL,
    END_TS          TIMESTAMP     NOT NULL,
    STOP_TYPE       VARCHAR(16)   NOT NULL,   -- PLANNED | UNPLANNED
    FAILURE_MODE    VARCHAR(32),
    PART_REPLACED   VARCHAR(32),
    CONSTRAINT PK_DOWNTIME PRIMARY KEY (STOP_ID)
);

CREATE OR REPLACE TABLE MAINT_NOTES (
    STOP_ID         DECIMAL(12,0) NOT NULL,
    NOTE_TEXT       VARCHAR(2000)
);

CREATE OR REPLACE TABLE SHIFT_PRODUCTION (
    MACHINE_ID       VARCHAR(32) NOT NULL,
    SHIFT_START      TIMESTAMP   NOT NULL,
    SHIFT_END        TIMESTAMP   NOT NULL,
    PLANNED_MINUTES  DOUBLE,
    DOWNTIME_MINUTES DOUBLE,
    IDEAL_CYCLE_S    DOUBLE,
    TOTAL_COUNT      DECIMAL(12,0),
    GOOD_COUNT       DECIMAL(12,0)
);

CREATE OR REPLACE TABLE SPARE_PARTS (
    PLANT_ID        VARCHAR(8)  NOT NULL,
    PART_NO         VARCHAR(32) NOT NULL,
    ON_HAND         DECIMAL(8,0),
    REORDER_POINT   DECIMAL(8,0),
    LEAD_TIME_DAYS  DECIMAL(4,0),
    UNIT_COST_INR   DECIMAL(12,0)
);

-- Spare-parts stock as each store's own system holds it (oee/stores.py).
-- Four systems, four layouts; sql/03_stores.sql reads them into SPARE_PARTS.
CREATE SCHEMA IF NOT EXISTS SAP_PNQ;
CREATE SCHEMA IF NOT EXISTS SAP_BLR;
CREATE SCHEMA IF NOT EXISTS TALLY_CHN;
CREATE SCHEMA IF NOT EXISTS WMS_HYD;
CREATE OR REPLACE TABLE SAP_PNQ.MARD (
    WERKS VARCHAR(8), MATNR VARCHAR(40), LABST DECIMAL(10,0), EISBE DECIMAL(10,0), PLIFZ DECIMAL(4,0), VERPR DECIMAL(12,2)
);
CREATE OR REPLACE TABLE SAP_BLR.MARD (
    WERKS VARCHAR(8), MATNR VARCHAR(40), LABST DECIMAL(10,0), EISBE DECIMAL(10,0), PLIFZ DECIMAL(4,0), VERPR DECIMAL(12,2)
);
CREATE OR REPLACE TABLE TALLY_CHN.STOCK_SUMMARY (
    ITEM_NAME VARCHAR(80), CLOSING_QTY DECIMAL(10,0), REORDER_LEVEL DECIMAL(10,0), LEAD_DAYS DECIMAL(4,0), RATE DECIMAL(12,2)
);
CREATE OR REPLACE TABLE WMS_HYD.INVENTORY (
    SKU VARCHAR(40), BIN_LOCATION VARCHAR(16), QTY_AVAILABLE DECIMAL(10,0), MIN_QTY DECIMAL(10,0),
    LEAD_TIME_D DECIMAL(4,0), UNIT_COST DECIMAL(12,2)
);
OPEN SCHEMA OEE;

-- Parts network: stores, road transfer times between them, and repair kits
CREATE OR REPLACE TABLE LOCATIONS (
    LOCATION_ID     VARCHAR(8)  NOT NULL,
    NAME            VARCHAR(64),
    KIND            VARCHAR(16)            -- PLANT | WAREHOUSE
);

CREATE OR REPLACE TABLE TRANSFER_LANES (
    FROM_LOCATION   VARCHAR(8)  NOT NULL,
    TO_LOCATION     VARCHAR(8)  NOT NULL,
    TRANSFER_HOURS  DECIMAL(6,1),
    COST_INR        DECIMAL(10,0)
);

CREATE OR REPLACE TABLE REPAIR_KITS (
    FAILURE_MODE    VARCHAR(32) NOT NULL,
    PART_NO         VARCHAR(32) NOT NULL,
    QTY             DECIMAL(6,0)
);

-- Parts reserved for open work orders (written by the console when a work order is raised)
CREATE TABLE IF NOT EXISTS PART_RESERVATIONS (
    WO_ID           DECIMAL(10,0) NOT NULL,
    PART_NO         VARCHAR(32)   NOT NULL,
    QTY             DECIMAL(6,0),
    FROM_LOCATION   VARCHAR(8),             -- NULL = ordered from the supplier
    TO_PLANT        VARCHAR(8),
    SOURCE_KIND     VARCHAR(16),            -- LOCAL | TRANSFER | SUPPLIER
    ETA_HOURS       DOUBLE,
    RESERVED_AT     TIMESTAMP,
    STATUS          VARCHAR(16)             -- RESERVED | RELEASED
);

-- Step 5: our part master and three supplier catalogs, each as delivered
CREATE OR REPLACE TABLE PART_MASTER (
    PART_NO         VARCHAR(32)  NOT NULL,
    DESCRIPTION     VARCHAR(200),
    CATEGORY        VARCHAR(16),
    MATCH_KEY       VARCHAR(32)             -- manufacturer code, normalised (A-Z, 0-9 only)
);

CREATE OR REPLACE TABLE CATEGORY_TERMS (
    CATEGORY        VARCHAR(16)  NOT NULL,
    TERM            VARCHAR(32)  NOT NULL   -- words suppliers use for the category ("BRG")
);

CREATE OR REPLACE TABLE SUPPLIERS (
    SUPPLIER_ID     VARCHAR(4)   NOT NULL,
    NAME            VARCHAR(64),
    CATALOG_FORMAT  VARCHAR(32),
    CURRENCY        VARCHAR(3)
);

CREATE OR REPLACE TABLE FX_RATES (
    CURRENCY        VARCHAR(3)   NOT NULL,
    INR_PER_UNIT    DOUBLE
);

-- Supplier A: CSV, INR, lead time in days
CREATE OR REPLACE TABLE SRC_SUPPLIER_A (
    ITEM_CODE       VARCHAR(32),
    ITEM_DESC       VARCHAR(200),
    PRICE_INR       DOUBLE,
    LEAD_DAYS       DECIMAL(4,0),
    MOQ             DECIMAL(6,0)
);

-- Supplier B: one JSON document per line, USD, lead time in weeks
CREATE OR REPLACE TABLE SRC_SUPPLIER_B (
    RAW_JSON        VARCHAR(2000)
);

-- Supplier C: text prices ("Rs. 1,234.50") and free-text delivery ("10 days", "3 wks")
CREATE OR REPLACE TABLE SRC_SUPPLIER_C (
    PARTNUMBER      VARCHAR(32),
    NAME            VARCHAR(200),
    RATE            VARCHAR(32),
    DELIVERY        VARCHAR(32)
);

-- Buyer-verified mapping of catalog lines to our parts (used only to score matching)
CREATE OR REPLACE TABLE SUPPLIER_MATCH_AUDIT (
    SUPPLIER_ID     VARCHAR(4),
    SUPPLIER_CODE   VARCHAR(32),
    TRUE_PART_NO    VARCHAR(32)
);

-- Registry of models compiled into UDFs (written by `oee deploy-udf`)
CREATE OR REPLACE TABLE MODEL_REGISTRY (
    MODEL_NAME      VARCHAR(64),
    DEPLOYED_AT     TIMESTAMP,
    N_TREES         DECIMAL(6,0),
    TEST_AUC        DOUBLE,
    TEST_PRECISION_AT_K DOUBLE,
    FEATURES        VARCHAR(2000),
    TRAIN_CUTOFF    TIMESTAMP          -- the model never saw data after this point
);

-- Work orders raised from the console (kept across pipeline re-runs)
CREATE TABLE IF NOT EXISTS WORK_ORDERS (
    WO_ID           DECIMAL(10,0) NOT NULL,
    MACHINE_ID      VARCHAR(32)   NOT NULL,
    PLANT_ID        VARCHAR(8)    NOT NULL,
    FAILURE_MODE    VARCHAR(32),
    PART_NO         VARCHAR(32),
    RISK_AT_OPEN    DOUBLE,
    OPENED_AT       TIMESTAMP,
    STATUS          VARCHAR(16)   NOT NULL   -- OPEN | CLOSED
);
