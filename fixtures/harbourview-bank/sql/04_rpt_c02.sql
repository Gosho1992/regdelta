-- owner: Regulatory Reporting
-- Reporting table for template C02 (counterparty register).
CREATE TABLE rpt_c02 AS
SELECT
    s.cpty_id,
    s.cpty_type,
    s.country_cd,
    s.legacy_rating,
    s.rpt_date
FROM stg_counterparty s;
