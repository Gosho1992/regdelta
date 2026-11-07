-- owner: Data Engineering
-- Staging: counterparty master data from CRM.
CREATE TABLE stg_counterparty AS
SELECT
    c.cpty_id,
    c.cpty_type,
    c.country_cd,
    c.legacy_rating,
    c.rpt_date
FROM crm.counterparties c;
