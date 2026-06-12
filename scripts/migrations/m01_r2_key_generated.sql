-- Migration m01: convert filings.r2_key to a GENERATED ALWAYS AS column.
--
-- Prerequisites:
--   filings.cik must already exist (added via prior migration).
--   filings must be empty — DROP + ADD is instant and lossless.
--
-- Run once manually against the live DB before first `discover`:
--   psql "$NEON_DIRECT_URL" -f scripts/migrations/m01_r2_key_generated.sql
--
-- Verify afterwards:
--   psql "$NEON_DIRECT_URL" -c "\d filings"
--   (r2_key should show as "generated always")

ALTER TABLE filings DROP COLUMN r2_key;
ALTER TABLE filings ADD COLUMN r2_key TEXT
    GENERATED ALWAYS AS ('filings/' || cik || '/' || accession_number || '.html') STORED;
