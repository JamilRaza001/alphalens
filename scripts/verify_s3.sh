#!/usr/bin/env bash
# Verifies all S3 acceptance criteria: ECR repo, lifecycle policy, idempotency,
# R2 lifecycle rule, log retention no-op, and shellcheck on all three scripts.
# Exits 0 only when every check passes.
# Usage (from repo root): bash scripts/verify_s3.sh
set -euo pipefail
set -a
# shellcheck source=/dev/null
source .env
set +a

REGION="ap-southeast-1"
REPO_NAME="${ECR_REPO_NAME:-alphalens-agent}"
PROFILE="${AWS_PROFILE:-alphalens-deployer}"
ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

PASS=0
FAIL=0

ok()  { echo "  PASS  $1"; PASS=$((PASS + 1)); }
err() { echo "  FAIL  $1"; FAIL=$((FAIL + 1)); }

echo ""
echo "=== S3 Cloud Resources Verification ==="
echo ""

# Prerequisite: jq
if ! command -v jq > /dev/null 2>&1; then
    echo "Error: jq is required. Install with: sudo apt install jq"
    exit 1
fi
if ! command -v shellcheck > /dev/null 2>&1; then
    echo "Error: shellcheck is required. Install with: sudo apt install shellcheck"
    exit 1
fi

# ------------------------------------------------------------------
# Check 1: ECR repo exists
# ------------------------------------------------------------------
echo "1. ECR repo exists..."
if aws ecr describe-repositories \
        --repository-names "$REPO_NAME" \
        --region "$REGION" \
        --profile "$PROFILE" \
        --output text > /dev/null 2>&1; then
    ok "ECR repo '${REPO_NAME}' found in ${REGION}"
else
    err "ECR repo '${REPO_NAME}' not found in ${REGION} — run create_ecr_repo.sh first"
fi

# ------------------------------------------------------------------
# Check 2: ECR lifecycle policy — countType + countNumber + action
# ------------------------------------------------------------------
echo "2. ECR lifecycle policy..."
policy_text=$(aws ecr get-lifecycle-policy \
    --repository-name "$REPO_NAME" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --query 'lifecyclePolicyText' \
    --output text 2>/dev/null || echo "")

if [[ -z "$policy_text" || "$policy_text" == "None" ]]; then
    err "ECR lifecycle policy not found"
else
    count_type=$(jq -r '.rules[0].selection.countType'   <<< "$policy_text")
    count_num=$(jq -r  '.rules[0].selection.countNumber' <<< "$policy_text")
    action=$(jq -r     '.rules[0].action.type'           <<< "$policy_text")
    if [[ "$count_type" == "imageCountMoreThan" && "$count_num" == "3" && "$action" == "expire" ]]; then
        ok "ECR lifecycle: countType=imageCountMoreThan, countNumber=3, action=expire"
    else
        err "ECR lifecycle mismatch — countType=${count_type}, countNumber=${count_num}, action=${action}"
    fi
fi

# ------------------------------------------------------------------
# Check 3: create_ecr_repo.sh idempotency (re-run must exit 0)
# ------------------------------------------------------------------
echo "3. create_ecr_repo.sh idempotency..."
if bash scripts/create_ecr_repo.sh > /dev/null 2>&1; then
    ok "create_ecr_repo.sh re-run exits 0"
else
    err "create_ecr_repo.sh re-run failed"
fi

# ------------------------------------------------------------------
# Check 4: R2 lifecycle rule — AbortIncompleteMultipartUpload after 7 days
# ------------------------------------------------------------------
echo "4. R2 lifecycle rule..."
r2_config=$(AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" \
    AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
    aws s3api get-bucket-lifecycle-configuration --region auto \
    --bucket "$R2_BUCKET" \
    --endpoint-url "$ENDPOINT" 2>/dev/null || echo "")

if [[ -z "$r2_config" ]]; then
    err "R2 lifecycle configuration not found — run setup_r2_lifecycle.sh first, or check R2 credentials"
else
    days=$(jq -r '
      .Rules[]
      | select(.ID == "abort-incomplete-multipart")
      | .AbortIncompleteMultipartUpload.DaysAfterInitiation
    ' <<< "$r2_config" 2>/dev/null || echo "")
    if [[ "$days" == "7" ]]; then
        ok "R2 lifecycle: AbortIncompleteMultipartUpload after 7 days"
    else
        err "R2 lifecycle DaysAfterInitiation expected 7, got '${days}'"
    fi
fi

# ------------------------------------------------------------------
# Check 5: set_log_retention.sh exits 0 (graceful no-op when no groups)
# ------------------------------------------------------------------
echo "5. set_log_retention.sh no-op..."
if bash scripts/set_log_retention.sh > /dev/null 2>&1; then
    ok "set_log_retention.sh exits 0 (no log groups is a graceful no-op)"
else
    err "set_log_retention.sh failed"
fi

# ------------------------------------------------------------------
# Check 6: shellcheck passes on all three operational scripts
# ------------------------------------------------------------------
echo "6. shellcheck..."
for script in \
    scripts/create_ecr_repo.sh \
    scripts/setup_r2_lifecycle.sh \
    scripts/set_log_retention.sh; do
    if shellcheck "$script"; then
        ok "shellcheck ${script}"
    else
        err "shellcheck ${script} — see warnings above"
    fi
done

# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="
echo ""

if [[ "$FAIL" -eq 0 ]]; then
    echo "All S3 acceptance criteria passed."
    exit 0
else
    echo "${FAIL} check(s) failed. See output above."
    exit 1
fi
