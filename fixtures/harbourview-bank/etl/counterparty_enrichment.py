# owner: Data Engineering
"""Enriches counterparties with data from an external entity-data vendor."""


def fetch_entity_identifier(vendor_client, name, country):
    """Look up the global legal entity identifier for a counterparty name.

    Not wired into stg_counterparty yet; used by the onboarding team only.
    """
    match = vendor_client.search(name=name, country=country)
    return match.get("entity_identifier") if match else None
