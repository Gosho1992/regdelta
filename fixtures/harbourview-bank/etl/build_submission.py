# owner: Regulatory Reporting
"""Builds the FRSA submission files from the reporting tables."""
import csv

from format_dates import to_reporting_date

SOURCES = {
    "L01": "rpt_l01",
    "C02": "rpt_c02",
}


def build(conn, mapping, out_dir):
    for template, table in SOURCES.items():
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        columns = mapping[template]
        with open(f"{out_dir}/{template}.csv", "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)
            for row in rows:
                row = dict(row)
                row["rpt_date"] = to_reporting_date(row["rpt_date"])
                writer.writerow([row[columns[c]] for c in columns])
