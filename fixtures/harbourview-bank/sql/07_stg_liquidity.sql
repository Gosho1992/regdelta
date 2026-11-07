-- owner: Treasury Data
-- Liquidity staging (not part of the PRF loan/counterparty templates).
CREATE TABLE stg_liquidity AS
SELECT
    t.position_id,
    t.rpt_date_local,
    t.hqla_amount,
    t.outflow_30d
FROM treasury.positions t;
