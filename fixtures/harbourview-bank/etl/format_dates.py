# owner: Data Engineering
"""Formats dates for regulatory output files."""
from datetime import date

# REPORTING_DATE is written in the regulator's required format.
REPORTING_DATE_FORMAT = "%d/%m/%Y"


def to_reporting_date(value: date) -> str:
    """Render rpt_date for the submission file."""
    return value.strftime(REPORTING_DATE_FORMAT)
