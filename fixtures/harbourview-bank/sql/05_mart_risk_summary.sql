-- owner: BI Team
-- Management mart: exposure by counterparty sector.
CREATE TABLE mart_risk_summary AS
SELECT
    c.cpty_type            AS sector,
    r.rpt_date,
    SUM(r.exposure_amt)    AS total_exposure,
    SUM(r.provision_amt)   AS total_provisions
FROM rpt_l01 r
JOIN rpt_c02 c ON c.cpty_id = r.cpty_id
GROUP BY c.cpty_type, r.rpt_date;
