# Scripts

Both read the market-signals-dbt controller tables (`database/controller/`) read-only. Run them with the project's
Python environment, which has duckdb.

- `build_site.py <repo root> <output folder>`: builds this site (the four HTML pages and `style.css`).
  Copy the output over the files at the top of this repo, then commit and push to update the site.
- `export_controller.py <repo root> <out.json>`: writes `controller_export.json`, the data file behind the
  private Claude dashboard. Upload it to the dashboard to refresh it.

`dashboard/index.html` is a copy of the private Claude dashboard's page (its HTML and scripts, no data). It only runs
inside that dashboard, which loads `controller_export.json` and the daily BI tables. The data isn't in this repo.
