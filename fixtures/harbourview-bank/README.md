# Harbourview Bank — synthetic reporting stack (FICTIONAL)

Everything in this folder is invented for the RegDelta demo. "FRSA" and the
"Prudential Reporting Framework (PRF)" are fictional; this is not a real
regulatory notice. Harbourview Bank does not exist.

The folder imitates a small bank's regulatory-reporting repository:

| Folder | What it holds |
|---|---|
| `specs/` | The regulator's previous (3.1) and revised (3.2) reporting specification |
| `data-dictionary.csv` | Regulatory field codes mapped to internal column names |
| `sql/` | SQL transformations (staging → reporting tables → marts) |
| `etl/` | Python ETL scripts |
| `reports/` | Regulatory, management and validation report definitions |
| `dashboards/` | BI dashboard configurations |
| `exports/` | The regulatory submission export mapping |
