# Data sources, licences, APIs and third-party tools

## Data

| Source | Licence | How it is used |
|---|---|---|
| Synthetic data from `oee/generate.py` | MIT (this repository) | All machines, telemetry, downtime, production counts, maintenance notes and spare-parts stock are generated. No employer, customer or proprietary data is used. |
| AI4I 2020 Predictive Maintenance Dataset (S. Matzka, UCI Machine Learning Repository) | CC BY 4.0 | Not redistributed or loaded. Its published failure-mode descriptions (tool wear, heat dissipation, power, overstrain) informed the generator's failure physics. |

## Platform

| Component | Licence | Role |
|---|---|---|
| Exasol Personal (database) | Proprietary, free for personal use ([EULA](https://www.exasol.com/terms-and-conditions/#h-exasol-personal-end-user-license-agreement)) | Data platform: storage, SQL analytics, window-function feature engineering, Python UDF execution. |
| Exasol Launcher (`exasol` CLI) | MIT | Installs and manages the local or cloud deployment. |
| Exasol python3 script language container | Exasol | Runtime for the `OEE.FAILURE_RISK` UDF (stock container, no custom packages). |

## Python libraries

| Library | Licence | Role |
|---|---|---|
| pyexasol | MIT | Database driver, parallel CSV import, pandas export |
| XGBoost | Apache 2.0 | Model training (offline, outside the database) |
| scikit-learn | BSD-3-Clause | Evaluation metrics |
| pandas | BSD-3-Clause | Data generation and offline feature reference |
| NumPy | BSD-3-Clause | Numerics |
| pytest | MIT | Test suite |

## APIs

No external APIs are called. The project runs fully offline once Exasol Personal is installed.
