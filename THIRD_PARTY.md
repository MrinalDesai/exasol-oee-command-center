# Data sources, licences, APIs and third-party tools

## Data

| Source | Licence | How it is used |
|---|---|---|
| Synthetic data from `oee/generate.py`, `oee/sources.py`, `oee/suppliers.py` | MIT (this repository) | All machines, sensor source systems, downtime, production counts, maintenance notes, spare-parts stock, stores, transfer lanes and supplier catalogs are generated. Supplier and brand names in the catalogs are invented; part codes follow public standards (ISO insert codes such as CNMG 120408, bearing series such as 6205). No employer, customer or proprietary data is used. |
| AI4I 2020 Predictive Maintenance Dataset (S. Matzka, UCI Machine Learning Repository) | CC BY 4.0 | Not redistributed or loaded. Its published failure-mode descriptions (tool wear, heat dissipation, power, overstrain) informed the generator's failure physics. |

## Platform

| Component | Licence | Role |
|---|---|---|
| Exasol Personal (database) | Proprietary, free for personal use ([EULA](https://www.exasol.com/terms-and-conditions/#h-exasol-personal-end-user-license-agreement)) | Data platform: storage, SQL analytics, window-function feature engineering, Python UDF execution. |
| Exasol Launcher (`exasol` CLI) | MIT | Installs and manages the local or cloud deployment. |
| Exasol python3 script language container | Exasol | Runtime for the `OEE.FAILURE_RISK`, `OEE.FAILURE_RISK_BATCH` and `OEE.QUALITY_RISK` UDFs (stock container, no custom packages). |

## Python libraries

| Library | Licence | Role |
|---|---|---|
| pyexasol | MIT | Database driver, parallel CSV import, pandas export |
| XGBoost | Apache 2.0 | Model training (offline, outside the database) |
| scikit-learn | BSD-3-Clause | Evaluation metrics |
| pandas | BSD-3-Clause | Data generation and offline feature reference |
| NumPy | BSD-3-Clause | Numerics |
| FastAPI | MIT | Console web backend |
| Uvicorn | BSD-3-Clause | ASGI server for the console |
| pytest | MIT | Test suite |
| httpx | BSD-3-Clause | Test client for the console API |
| DuckDB | MIT | Tests only: runs the project's SQL as an offline stand-in for Exasol |

## Fonts

| Font | Licence | Role |
|---|---|---|
| Inter, JetBrains Mono (Google Fonts) | SIL Open Font License 1.1 | Console typography; the page falls back to system fonts when offline |

## APIs

No external APIs are called. The only network request besides Exasol is the console's font stylesheet from Google Fonts, which is optional.
