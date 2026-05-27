#!/usr/bin/env bash
# Applies an AbortIncompleteMultipartUpload lifecycle rule to the AlphaLens R2
# bucket, cleaning up abandoned multipart uploads after 7 days.
# Idempotent: put-bucket-lifecycle-configuration replaces existing rules each run.
# Usage (from repo root): bash scripts/setup_r2_lifecycle.sh
set -euo pipefail
set -a
# shellcheck source=/dev/null
source .env
set +a

ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

echo "R2 bucket: ${R2_BUCKET} | endpoint: ${ENDPOINT}"

# Apply lifecycle rule — R2 supports AbortIncompleteMultipartUpload and Expiration;
# Transition rules are NOT supported (R2 has no storage classes). Use only multipart cleanup.
AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" \
    AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
    aws s3api put-bucket-lifecycle-configuration --region auto \
    --bucket "$R2_BUCKET" \
    --endpoint-url "$ENDPOINT" \
    --lifecycle-configuration '{
      "Rules": [{
        "ID": "abort-incomplete-multipart",
        "Status": "Enabled",
        "Filter": { "Prefix": "" },
        "AbortIncompleteMultipartUpload": { "DaysAfterInitiation": 7 }
      }]
    }'

echo "Lifecycle rule applied. Verifying:"

# Print the applied configuration to stdout
AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" \
    AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
    aws s3api get-bucket-lifecycle-configuration --region auto \
    --bucket "$R2_BUCKET" \
    --endpoint-url "$ENDPOINT"
