# Global Economic Intelligence Platform

Project brief  •  Beginner-friendly  •  Zero additional infrastructure budget

## Objective
Build an automated data platform that collects, standardizes and analyzes economic indicators across countries. Focus on foundational data engineering skills and a reproducible pipeline rather than a complex frontend.

## Public data sources
- World Bank Open Data / API — data.worldbank.org (GDP, inflation, unemployment and population).
- FRED — fred.stlouisfed.org (additional economic series; free API key may be required).
- IMF Data — data.imf.org (optional additional source).

## Features to implement
- Automated ingestion: fetch historical data from the World Bank first, then integrate FRED. Preserve raw responses and ingestion timestamps.
- Data cleaning and modeling: standardize country codes, indicator IDs, units and reporting periods; handle missing values and duplicates. Build country, indicator and observation tables.
- Incremental updates: schedule refreshes, support historical backfills and retain previous values when sources revise observations.
- Data quality: validate keys, units and date ranges; reconcile record counts, log failures and make reruns idempotent.
- Analytics: compare countries and economic trends through a dashboard with filters for country, indicator and period. Clearly distinguish units and reporting frequencies.

## Suggested technology stack
Python (requests, Pandas) · PostgreSQL · dbt Core · Apache Airflow · Metabase OSS · pytest · Docker Compose or Podman. Start with Python and PostgreSQL before introducing orchestration and transformation tools.

## Expected deliverables
- Working ingestion and transformation pipelines, SQL data models and automated quality tests.
- Dashboard covering GDP, inflation, unemployment and population; README with local setup, architecture and pipeline execution instructions.
- Initial scope: 10 countries, four indicators and 10 years of available data; expand after the pipeline is reliable. Optional: IMF integration and anomaly detection.
Constraint: use public data and free, locally runnable tools; no paid cloud services required.
## Running the ingestion (World Bank CSVs)

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
createdb geip                                    # once
export DATABASE_URL=postgresql://postgres:<password>@localhost:5432/geip

.venv/bin/python ingest.py --dry-run             # parse + validate, no database
.venv/bin/python ingest.py --init-db             # create tables and load
.venv/bin/python ingest.py                       # rerun: unchanged files are skipped
.venv/bin/python ingest.py --start-year 2000     # backfill a wider window
.venv/bin/pytest tests                           # DB tests need TEST_DATABASE_URL (a throwaway DB)
```

Target countries are ISO3 codes in `countries`. Each `datasources/<folder>/` holds one World Bank download
(`API_*.csv` + its two `Metadata_*` files); the folder name doesn't matter.

Tables: `raw.files` (one row per file, sha256, source date, status), `raw.observations` (every value as parsed),
`core.country`, `core.indicator` (with unit and frequency), `core.observation` (target countries and years),
`core.observation_history` (previous values when a source revises a number).
