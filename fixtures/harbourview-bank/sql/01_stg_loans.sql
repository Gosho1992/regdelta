-- owner: Data Engineering
-- Staging: loan book extract from the core banking system.
CREATE TABLE stg_loans AS
SELECT
    l.loan_id,
    l.cpty_id,
    l.rpt_date,
    l.exposure_amt,
    l.provision_amt
FROM core_banking.loans l
WHERE l.status = 'ACTIVE';
