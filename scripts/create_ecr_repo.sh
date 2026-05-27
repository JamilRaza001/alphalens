#!/usr/bin/env bash
# Creates ECR repo "alphalens-agent" in ap-southeast-1 with image scanning enabled
# and a lifecycle policy keeping only the 3 most recent images.
# Idempotent: skips creation if repo exists, always re-applies lifecycle policy.
# Usage (from repo root): bash scripts/create_ecr_repo.sh
set -euo pipefail
set -a
# shellcheck source=/dev/null
source .env
set +a

REGION="ap-southeast-1"
REPO_NAME="${ECR_REPO_NAME:-alphalens-agent}"
PROFILE="${AWS_PROFILE:-alphalens-deployer}"

echo "ECR repo: ${REPO_NAME} | region: ${REGION} | profile: ${PROFILE}"

# Create repo only if it does not already exist
if aws ecr describe-repositories \
        --repository-names "$REPO_NAME" \
        --region "$REGION" \
        --profile "$PROFILE" \
        --output text > /dev/null 2>&1; then
    echo "Repository already exists — skipping create."
else
    aws ecr create-repository \
        --repository-name "$REPO_NAME" \
        --region "$REGION" \
        --profile "$PROFILE" \
        --image-scanning-configuration scanOnPush=true \
        --image-tag-mutability MUTABLE \
        --output text > /dev/null
    echo "Repository created."
fi

# Always re-apply lifecycle policy (put-lifecycle-policy is an idempotent replace)
aws ecr put-lifecycle-policy \
    --repository-name "$REPO_NAME" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --lifecycle-policy-text '{
      "rules": [{
        "rulePriority": 1,
        "description": "Keep last 3 images, expire older",
        "selection": {
          "tagStatus": "any",
          "countType": "imageCountMoreThan",
          "countNumber": 3
        },
        "action": { "type": "expire" }
      }]
    }' \
    --output text > /dev/null
echo "Lifecycle policy applied: keep last 3 images."

# Print repo URI to stdout
repo_uri=$(aws ecr describe-repositories \
    --repository-names "$REPO_NAME" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --query 'repositories[0].repositoryUri' \
    --output text)
echo "Repository URI: ${repo_uri}"
