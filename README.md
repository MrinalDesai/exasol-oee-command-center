# OEE Command Center on Exasol Personal

Predictive maintenance and OEE analytics for a multi-plant machine fleet, where
**the analytics and the model both run inside Exasol**: OEE, MTBF and MTTR are
SQL views, features are computed with window functions, and a gradient-boosted
failure-risk model is compiled into a dependency-free Python UDF that scores
every reading in-database.

Built for ExaThon India 2026, Track 03: Factories, Fleets & Supply Chains.

## Problem

**Who has it:** maintenance and production managers running mixed fleets of
CNC mills, lathes, presses and packing machines across several plants.

**The problem:** unplanned downtime arrives with no warning and no clear root
cause. Sensor telemetry, downtime logs, production counts and spare-parts stock
sit in separate systems, so nobody can answer three questions in one place:

1. Where is OEE being lost: availability, performance or quality?
2. Which machines are likely to fail in the next 24 hours?
3. Are the spare parts those failures will need actually at the right plant?

**Why it matters:** an unplanned stop costs hours of lost output, and a stop
that also waits on a part from another site costs days. Knowing risk 24 hours
ahead turns a breakdown into a planned intervention.

**Outcome:** one Exasol schema that answers all three questions, with every
number traceable to SQL.

## How it uses Exasol

| Capability | Where |
|---|---|
| Parallel bulk load via pyexasol HTTP import | `oee/load.py` |
| OEE (A×P×Q), MTBF, MTTR, inherent availability, downtime Pareto as SQL views, with correct weighting (components are summed, never ratios averaged) | `sql/02_oee_views.sql` |
| Feature engineering in-database with analytic window functions (`AVG … ROWS BETWEEN`, `LAG`) | `sql/03_features.sql` |
| Training labels in-database (unplanned stop within 24 h, correlated `EXISTS` + `ADD_HOURS`) | `sql/03_features.sql` |
| Model inference as a **PYTHON3 SCALAR UDF** on the stock script container | `oee/compile_udf.py` → `OEE.FAILURE_RISK` |
| Decision views joining risk × historical failure-mode mix × stock on hand | `sql/04_scoring.sql` (`V_PARTS_EXPOSURE`) |
| Model registry table | `OEE.MODEL_REGISTRY` |

### Why the model is compiled into the UDF

XGBoost is trained outside the database, then its trees are flattened into
compact arrays and embedded with a ~50-line pure-Python scorer into a single
`CREATE PYTHON3 SCALAR SCRIPT` statement. That means:

- no BucketFS upload and no custom script language container to build;
- it runs on any Exasol Personal deployment with the standard python3 container;
- scoring happens where the data lives, in parallel UDF instances.

`tests/test_udf_parity.py` executes the generated UDF body and checks it against
`xgboost.predict_proba`: maximum absolute difference is below 1e-6 on the full
dataset.

## Architecture

```mermaid
flowchart LR
    G[oee/generate.py<br/>synthetic fleet] -->|pyexasol import| T[(OEE tables<br/>TELEMETRY, DOWNTIME,<br/>SHIFT_PRODUCTION,<br/>SPARE_PARTS ...)]
    T --> V1[V_OEE_* / V_RELIABILITY<br/>OEE · MTBF · MTTR]
    T --> V2[V_FEATURES / V_TRAINING<br/>window-function features]
    V2 -->|export_to_pandas| TR[oee/train.py<br/>XGBoost]
    TR --> C[oee/compile_udf.py]
    C -->|CREATE PYTHON3 SCALAR SCRIPT| U[[OEE.FAILURE_RISK UDF]]
    V2 --> U
    U --> R[(RISK_SCORES)]
    R --> D[V_RISK_LATEST<br/>V_PARTS_EXPOSURE]
    V1 --> D
```

## Quick start

Requirements: Python 3.10+, and Exasol Personal (local on macOS / Linux / Windows,
or on AWS / Azure).

```bash
# 1. Exasol Personal (local)
curl https://www.exasol.com/install/ | sh      # installs the `exasol` launcher
exasol install local
exasol slc install python3                     # local installs ship without UDF runtimes
exasol info                                    # shows the DSN to connect to

# 2. This project
git clone <this repo> && cd exasol-oee-command-center
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                           # set EXA_DSN from `exasol info`

# 3. Run the whole pipeline
python -m oee.cli all
```

`all` runs these steps, which can also be run one at a time:

| Command | What it does |
|---|---|
| `python -m oee.cli generate` | Writes the synthetic dataset to `data/generated/` |
| `python -m oee.cli setup` | Creates the schema, loads the data, creates OEE / reliability / feature views |
| `python -m oee.cli train` | Pulls `OEE.V_TRAINING` from Exasol, trains XGBoost, writes `models/` |
| `python -m oee.cli deploy` | Compiles the model into `OEE.FAILURE_RISK`, scores every reading, builds decision views |
| `python -m oee.cli status` | Prints plant OEE, highest-risk machines and spare parts at risk |

Cloud deployment: replace `exasol install local` with `exasol install aws` or
`exasol install azure` (see the [Exasol Personal README](https://github.com/exasol/exasol-personal)).
Cloud deployments include the python3 container already.

### Example queries

```sql
-- Where is OEE lost, by plant?
SELECT * FROM OEE.V_OEE_PLANT ORDER BY OEE;

-- Which machines need attention in the next 24 hours?
SELECT MACHINE_ID, RISK_24H, MTBF_HOURS, MTTR_HOURS
FROM OEE.V_RISK_LATEST ORDER BY RISK_24H DESC LIMIT 10;

-- Score any reading ad hoc, in-database
SELECT MACHINE_ID, READING_TS,
       OEE.FAILURE_RISK(AIR_TEMP_K, PROCESS_TEMP_K, TEMP_DIFF_K, RPM, TORQUE_NM, TORQUE_RATIO,
                        TOOL_WEAR_MIN, WEAR_X_LOAD, VIBRATION_MM_S, POWER_KW, VIB_AVG_6H,
                        VIB_DELTA_6H, TEMP_DIFF_AVG_6H, POWER_AVG_6H,
                        IS_MILL, IS_LATHE, IS_PRESS, IS_PACKER) AS RISK
FROM OEE.V_FEATURES WHERE MACHINE_ID = 'PNQ-MILL-01' ORDER BY READING_TS DESC LIMIT 24;

-- Are the parts we are about to need at the right plant?
SELECT * FROM OEE.V_PARTS_EXPOSURE WHERE STATUS <> 'OK';
```

## Data

All data is synthetic, generated by `oee/generate.py` (seeded, reproducible):
24 machines across Pune, Bengaluru and Chennai, 60 days of hourly telemetry
(~34k readings), planned and unplanned stops with technician notes, per-shift
production counts, and spare-parts stock per plant. Failure behaviour follows
five competing modes (tool wear, heat dissipation, power, overstrain, bearing
vibration). See [THIRD_PARTY.md](THIRD_PARTY.md) for data provenance and licences.

## Model

Binary classifier: *will this machine have an unplanned stop in the next 24 hours?*
Time-based split (last 20% of the period held out). On the default seed:

| Metric | Value |
|---|---|
| Test ROC AUC | 0.80 |
| Test average precision | 0.46 |
| Precision in top 5% of risk | 0.64 (base rate 0.086, ~7× lift) |

These figures are on synthetic data and show the pipeline works end to end;
they are not a claim about real plant performance.

## Repository layout

```
oee/
  generate.py      synthetic data generator
  features.py      feature contract (FEATURES) + pandas reference implementation
  train.py         XGBoost training and evaluation
  scorer.py        pure-Python tree scorer (embedded into the UDF)
  compile_udf.py   booster -> CREATE PYTHON3 SCALAR SCRIPT
  load.py          CSV -> Exasol via pyexasol
  sqlrun.py        SQL file runner (handles UDF '/' terminators)
  db.py            connection settings
  cli.py           pipeline commands
sql/
  01_schema.sql  02_oee_views.sql  03_features.sql  04_scoring.sql
tests/             pytest suite (data, features, SQL contract, UDF parity)
```

## Tests

```bash
pytest -q
```

The suite runs offline (no database needed): generator determinism and
integrity, OEE component bounds, label correctness, SQL ↔ Python feature
contract, the SQL statement splitter, and UDF ↔ XGBoost parity.

## Licence

MIT, see [LICENSE](LICENSE). Exasol Personal itself is proprietary software,
free for personal use under its own EULA.
