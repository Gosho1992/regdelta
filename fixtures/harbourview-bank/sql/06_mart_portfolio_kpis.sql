-- owner: Finance BI
-- Board KPI mart, built from the general ledger rather than the loan book.
-- total_risk_amount is the gross carrying amount of the credit portfolio.
CREATE TABLE mart_portfolio_kpis AS
SELECT
    g.period_end,
    SUM(g.gross_balance)   AS total_risk_amount
FROM finance.gl_balances g
WHERE g.account_class = 'LOANS'
GROUP BY g.period_end;
