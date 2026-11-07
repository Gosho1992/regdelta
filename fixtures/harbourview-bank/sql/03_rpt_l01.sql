-- owner: Regulatory Reporting
-- Reporting table for template L01 (loan-level exposures).
CREATE TABLE rpt_l01 AS
SELECT
    s.loan_id,
    s.cpty_id,
    s.rpt_date,
    s.exposure_amt AS exposure_amt,      -- gross, before provisions
    s.provision_amt
FROM stg_loans s;
