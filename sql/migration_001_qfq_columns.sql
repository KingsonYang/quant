-- Migration: Add QFQ (front-adjusted) OHLC columns and adjust_type to daily_bar
-- Run this against the existing quant.duckdb to add the new columns.
-- Existing rows will have NULL qfq values until backfilled via backfill-prices command.

ALTER TABLE daily_bar ADD COLUMN IF NOT EXISTS open_qfq DECIMAL(18,4);
ALTER TABLE daily_bar ADD COLUMN IF NOT EXISTS high_qfq DECIMAL(18,4);
ALTER TABLE daily_bar ADD COLUMN IF NOT EXISTS low_qfq DECIMAL(18,4);
ALTER TABLE daily_bar ADD COLUMN IF NOT EXISTS close_qfq DECIMAL(18,4);
ALTER TABLE daily_bar ADD COLUMN IF NOT EXISTS adjust_type VARCHAR DEFAULT 'none';
