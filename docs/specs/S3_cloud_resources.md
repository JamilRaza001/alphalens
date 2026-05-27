\# Spec S3 — cloud\_resources

\## Goal

Provision and configure the three persistent cloud resources AlphaLens requires before any application code runs in cloud: (1) a single ECR repository for the merged agent+reranker container with native lifecycle policy retaining only the last 3 images, (2) a Cloudflare R2 bucket lifecycle rule auto-cleaning abandoned multipart uploads after 7 days, (3) a CloudWatch log retention script that idempotently sets 7-day retention on every \`/aws/lambda/alphalens-\*\` log group. All operations are CLI-driven (no Console clicks), region-pinned to \`ap-southeast-1\` for AWS resources, and safe to re-run.

\## Function Signatures

\### \`scripts/create\_ecr\_repo.sh\`

\`\`\`bash

\# Creates ECR repo "alphalens-agent" in ap-southeast-1 with image scanning

\# enabled and a lifecycle policy keeping only the 3 most recent images.

\# Idempotent: skips creation if repo exists, always re-applies lifecycle.

\# Requires: AWS\_PROFILE=alphalens-admin, aws CLI v2.

\# Outputs: repo URI to stdout, exit 0 on success.

bash scripts/create\_ecr\_repo.sh

\`\`\`

\### \`scripts/setup\_r2\_lifecycle.sh\`

\`\`\`bash

\# Applies a lifecycle rule to the AlphaLens R2 bucket that aborts incomplete

\# multipart uploads older than 7 days. Uses \`aws s3api\` against the R2

\# S3-compatible endpoint.

\# Idempotent: replaces existing rule each run.

\# Requires: env vars R2\_ACCOUNT\_ID, R2\_ACCESS\_KEY\_ID, R2\_SECRET\_ACCESS\_KEY,

\# R2\_BUCKET (loaded from .env via \`set -a; source .env; set +a\`).

\# Outputs: applied rule JSON to stdout, exit 0 on success.

bash scripts/setup\_r2\_lifecycle.sh

\`\`\`

\### \`scripts/set\_log\_retention.sh\`

\`\`\`bash

\# Sets 7-day retention on every CloudWatch log group with prefix

\# "/aws/lambda/alphalens-" in ap-southeast-1. Safe when zero groups exist

\# (no-op). Re-runnable. Use after every Lambda deploy.

\# Requires: AWS\_PROFILE=alphalens-admin, aws CLI v2.

\# Outputs: one "set retention=7 on " line per group, exit 0.

bash scripts/set\_log\_retention.sh

\`\`\`

\### \`scripts/verify\_s3.sh\`

\`\`\`bash

\# Verifies all S3 acceptance criteria: ECR repo present, lifecycle policy

\# correct, R2 lifecycle applied, log retention script runs cleanly,

\# shellcheck passes on all three scripts.

\# Exits 0 only if all checks pass.

bash scripts/verify\_s3.sh

\`\`\`

\## Acceptance Criteria

1\. \`scripts/create\_ecr\_repo.sh\` exits 0 on first run; \`aws ecr describe-repositories --repository-names alphalens-agent --region ap-southeast-1\` returns the repo metadata.

2\. \`aws ecr get-lifecycle-policy --repository-name alphalens-agent --region ap-southeast-1\` returns a JSON policy with rule \`countType: imageCountMoreThan\`, \`countNumber: 3\`, action \`expire\`.

3\. Re-running \`scripts/create\_ecr\_repo.sh\` exits 0 and does NOT error on existing repo (idempotency check).

4\. \`scripts/setup\_r2\_lifecycle.sh\` exits 0; \`aws s3api get-bucket-lifecycle-configuration --bucket "$R2\_BUCKET" --endpoint-url "https://$R2\_ACCOUNT\_ID.r2.cloudflarestorage.com"\` returns a rule containing \`AbortIncompleteMultipartUpload\` with \`DaysAfterInitiation: 7\`.

5\. \`scripts/set\_log\_retention.sh\` exits 0 even when no \`/aws/lambda/alphalens-\*\` log groups exist yet (graceful no-op).

6\. All three scripts begin with \`set -euo pipefail\` and pass \`shellcheck\` with no warnings.

7\. \`scripts/verify\_s3.sh\` runs all checks above and exits 0.

8\. \`docs/PROJECT\_STATUS.md\` S3 row updated \`IN\_PROGRESS → DONE\` with commit hash after merge.

\## Gotchas

\- \*\*R2 lifecycle quirk:\*\* R2 supports a subset of S3 lifecycle rules. \`AbortIncompleteMultipartUpload\` and object \`Expiration\` work; \`Transition\` rules do NOT (R2 has no storage classes). Use the multipart cleanup rule only — anything else will silently no-op or error.

\- \*\*ECR native lifecycle = no cron needed:\*\* This resolves open decision \*\*O10\*\* simply. AWS evaluates the lifecycle policy on every image push, so no GitHub Action or Lambda cron is required for cleanup. Do NOT add a separate workflow for this.

\- \*\*CloudWatch log groups don't exist until first invocation:\*\* Lambda auto-creates \`/aws/lambda/\` on its first call, not at function creation. So \`set\_log\_retention.sh\` is a no-op until S5 (Lambda deploy + smoke invoke). The script must handle the empty-list case cleanly — that's why criterion #5 is explicit.
