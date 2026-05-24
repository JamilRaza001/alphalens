#!/usr/bin/env bash
# Verify S2: db schema + seed. Run from repo root: bash "scripts/verify_s2.sh"
set -euo pipefail

# Load DATABASE_URL from .env
set -a; source .env; set +a

PSQL="psql $DATABASE_URL -v ON_ERROR_STOP=1 -tAc"

echo "1. Extensions..."
$PSQL "SELECT extname FROM pg_extension WHERE extname IN ('vector','pgcrypto');" | wc -l | grep -q '^2$'

echo "2. Tables..."
for t in companies filings chunks queries ingestion_jobs; do
    $PSQL "SELECT to_regclass('public.$t');" | grep -q "^$t$"
done

echo "3. embedding column type..."
$PSQL "SELECT format_type(atttypid, atttypmod) FROM pg_attribute
WHERE attrelid='chunks'::regclass AND attname='embedding';" | grep -q 'vector(768)'

echo "4. HNSW index..."
$PSQL "SELECT indexdef FROM pg_indexes WHERE indexname='idx_chunks_embedding_hnsw';" | grep -q 'hnsw'

echo "5. GIN index..."
$PSQL "SELECT indexdef FROM pg_indexes WHERE indexname='idx_chunks_tsv_gin';" | grep -q 'gin'

echo "6. Seed count = 10..."
$PSQL "SELECT count(*) FROM companies;" | grep -q '^10$'

echo "✅ S2 verification passed."
