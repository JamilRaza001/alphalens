#!/usr/bin/env bash
# Sets 7-day retention on every CloudWatch log group matching /aws/lambda/alphalens-*
# in ap-southeast-1. Safe no-op when no matching groups exist (Lambda hasn't been
# invoked yet — groups are created on first invocation, not at function creation).
# Re-runnable: put-retention-policy is idempotent.
# Usage (from repo root): bash scripts/set_log_retention.sh
set -euo pipefail
set -a
# shellcheck source=/dev/null
source .env
set +a

REGION="ap-southeast-1"
PROFILE="${AWS_PROFILE:-alphalens-deployer}"
RETENTION_DAYS=7
PREFIX="/aws/lambda/alphalens-"

echo "Scanning for log groups with prefix: ${PREFIX}"

# --output text returns "None" (literal) when query result is empty.
log_groups_raw=$(aws logs describe-log-groups \
    --log-group-name-prefix "$PREFIX" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --query 'logGroups[*].logGroupName' \
    --output text)

if [[ -z "$log_groups_raw" || "$log_groups_raw" == "None" ]]; then
    echo "No matching log groups found — no-op (expected before first Lambda deploy)."
    exit 0
fi

# AWS CLI --output text tab-separates list items on one line; convert to newlines.
while IFS= read -r log_group; do
    [[ -z "$log_group" ]] && continue
    aws logs put-retention-policy \
        --log-group-name "$log_group" \
        --retention-in-days "$RETENTION_DAYS" \
        --region "$REGION" \
        --profile "$PROFILE"
    echo "set retention=${RETENTION_DAYS} on ${log_group}"
done < <(tr '\t' '\n' <<< "$log_groups_raw")
