# AlphaLens — Phase 1 Complete Setup Guide

**Version:** v8 (aligned with AlphaLens_v8.md)
**Goal:** Take you from zero to a fully bootstrapped project — environment, all services, billing protection, IAM, repo, schema, and a green smoke test — so Phase 2 (Claude Code spec implementation) can start immediately.

> ✅ = do it once and tick it off
> ⚠️ = easy to get wrong — read carefully before executing
> 💡 = context / reasoning you should understand

---

## Prerequisites Checklist (Before You Start)

- [ ] HBL Visa/Mastercard debit — international transactions ON (Settings → Card Management)
- [ ] PKR ~1000 buffer in account (AWS will pre-authorize $1, refunded in 3–5 days)
- [ ] OTP/SMS working on your registered number
- [ ] WSL2 already installed (we'll verify in Part 1)
- [ ] VS Code already installed on Windows
- [ ] ~8 hours total time (Day 1: Parts 1–4, Day 2: Parts 5–8)

---

## Part 0 — How to Read This Guide

Each step is formatted as:

```
[WHAT] short title
[WHERE] Windows terminal / WSL terminal / Browser / AWS Console
[CMD] exact command to run (if applicable)
[VERIFY] how to confirm it worked
```

Never run a command without reading the `[VERIFY]` step first. If verification fails, stop — do not continue to the next step.

---

# DAY 1

---

## Part 1 — Local Environment

All development happens inside WSL2 (Ubuntu). Never install Python or project dependencies directly on Windows.

---

### 1.1 — Verify WSL2

```
[WHERE] Windows PowerShell (run as Administrator)
[CMD]   wsl --status
[VERIFY] Output should say:
         Default Distribution: Ubuntu
         Default Version: 2
```

If it says Version: 1, run: `wsl --set-default-version 2`

```
[CMD]   wsl --list --verbose
[VERIFY] Ubuntu row shows VERSION = 2, STATE = Running
```

---

### 1.2 — Enter WSL and Update System

```
[WHERE] WSL terminal (open Ubuntu from Start menu)
[CMD]
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl git build-essential libssl-dev pkg-config unzip

[VERIFY] No errors. `git --version` should print git 2.x.x
```

---

### 1.3 — Install Python 3.12 (if not present)

```
[WHERE] WSL terminal
[CMD]   python3 --version
[VERIFY] Should print Python 3.12.x
```

If not 3.12:

```
[CMD]
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev
sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1

[VERIFY] python3 --version → Python 3.12.x
```

---

### 1.4 — Install uv (Python package manager)

💡 `uv` replaces pip/poetry. It's 10–100× faster and manages lockfiles properly. All dependency commands in this project use `uv`.

```
[WHERE] WSL terminal
[CMD]   curl -LsSf https://astral.sh/uv/install.sh | sh
        source $HOME/.cargo/env   # add to PATH for this session

[VERIFY] uv --version → uv 0.x.x
```

Add to shell permanently:

```
[CMD]
echo 'export PATH="$HOME/.cargo/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

[VERIFY] uv --version works in a new terminal
```

---

### 1.5 — Install Node.js via nvm

💡 Node is needed for the Next.js frontend. Use nvm (Node Version Manager) — never install Node directly via apt.

```
[WHERE] WSL terminal
[CMD]
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
source ~/.bashrc
nvm install 20
nvm use 20
nvm alias default 20

[VERIFY]
node --version  → v20.x.x
npm --version   → 10.x.x
```

---

### 1.6 — Install AWS CLI v2

```
[WHERE] WSL terminal
[CMD]
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
sudo ./aws/install
rm -rf aws awscliv2.zip

[VERIFY] aws --version → aws-cli/2.x.x
```

---

### 1.7 — Install Docker

⚠️ Docker Desktop on Windows has WSL2 integration. If you already have Docker Desktop installed on Windows, it exposes `docker` inside WSL automatically.

```
[WHERE] WSL terminal
[CMD]   docker --version
[VERIFY] Docker version 26.x.x or higher
```

If not found — install Docker Desktop on Windows from https://www.docker.com/products/docker-desktop, enable WSL2 integration in Settings → Resources → WSL Integration → Ubuntu ON. Restart WSL after.

```
[VERIFY] docker ps   → empty table (daemon is running)
```

---

### 1.8 — VS Code WSL Extension

```
[WHERE] VS Code on Windows
[ACTION] Install extension: "WSL" (ms-vscode-remote.remote-wsl)
[CMD]    In WSL terminal: code --version
[VERIFY] VS Code version printed (WSL integration working)
```

---

### 1.9 — Install Git and Configure

```
[WHERE] WSL terminal
[CMD]
git config --global user.name "Your Name"
git config --global user.email "your@email.com"
git config --global init.defaultBranch main

[VERIFY] git config --list | grep user → shows your name and email
```

---

### ✅ Part 1 Done

```
[VERIFY ALL]
python3 --version    → 3.12.x
uv --version         → 0.x.x
node --version       → 20.x.x
aws --version        → 2.x.x
docker --version     → 26.x.x
git --version        → 2.x.x
```

---

## Part 2 — Service Signups

Do all of these in browser. After each signup, save the API key in a local text file (we'll put them into `.env` properly in Part 5).

---

### 2.1 — AWS Account (Already Done ✅)

You already have:
- [ ] AWS account created
- [ ] Root MFA enabled
- [ ] $100 credits applied

⚠️ **Do NOT use root credentials for any CLI or deploy work.** Root is only for billing and emergency. You'll create IAM users in Part 4.

---

### 2.2 — Neon Postgres

```
[WHERE] Browser → https://neon.tech
[ACTION]
1. Sign up with GitHub (easier SSO)
2. Create project: name = "alphalens", region = "AWS ap-southeast-1 (Singapore)"
3. After creation, go to: Project → Connection Details
4. Copy TWO connection strings:
   - Pooled (for application):   postgresql://user:pass@ep-xxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
   - Direct (for migrations):    postgresql://user:pass@ep-xxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&endpoint=ep-xxx

[SAVE]
NEON_DATABASE_URL=<pooled connection string>
NEON_DIRECT_URL=<direct connection string>

[VERIFY]
Install psql in WSL: sudo apt install -y postgresql-client
psql "$NEON_DATABASE_URL" -c "SELECT version();"
→ Should print PostgreSQL 16.x
```

💡 **Why two URLs?** The pooled URL goes through PgBouncer (connection pooling) — it's what the app uses in Lambda to avoid exhausting Postgres connections. But PgBouncer runs in "transaction mode" which breaks `SET` commands, advisory locks, and other session-level features that Alembic (migrations tool) needs. So migrations always use the direct URL.

---

### 2.3 — Enable pgvector on Neon

```
[WHERE] Neon dashboard → SQL Editor (or psql from WSL)
[CMD]
CREATE EXTENSION IF NOT EXISTS vector;
SELECT extversion FROM pg_extension WHERE extname = 'vector';

[VERIFY] Returns: 0.7.x or higher
```

---

### 2.4 — Cloudflare R2

```
[WHERE] Browser → https://dash.cloudflare.com
[ACTION]
1. Sign up / log in
2. Add payment method (required even for free tier — won't be charged)
3. Left sidebar → R2 Object Storage → Create bucket
   Bucket name: alphalens-filings
   Region: Automatic (Cloudflare picks nearest)
4. Go to R2 → Manage R2 API Tokens → Create API Token
   Permissions: Object Read & Write
   Scope: Specific bucket → alphalens-filings
5. Copy the credentials shown (only shown once):

[SAVE]
R2_ACCOUNT_ID=<your Cloudflare account ID>   # found in right sidebar of R2 page
R2_ACCESS_KEY_ID=<token access key>
R2_SECRET_ACCESS_KEY=<token secret key>
R2_BUCKET_NAME=alphalens-filings
R2_ENDPOINT_URL=https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com

[VERIFY]
# Install awscli S3 compat check:
AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID \
AWS_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY \
aws s3 ls s3://alphalens-filings \
  --endpoint-url $R2_ENDPOINT_URL \
  --region auto
→ Empty output is fine (bucket exists, no files yet)
```

---

### 2.5 — Groq

```
[WHERE] Browser → https://console.groq.com
[ACTION]
1. Sign up with GitHub
2. Left sidebar → API Keys → Create API Key
   Name: alphalens-v1

[SAVE]
GROQ_API_KEY=gsk_...

[VERIFY]
curl https://api.groq.com/openai/v1/models \
  -H "Authorization: Bearer $GROQ_API_KEY" | python3 -m json.tool | grep "llama-3.3"
→ Should show llama-3.3-70b-versatile in the list
```

---

### 2.6 — Jina AI

```
[WHERE] Browser → https://jina.ai
[ACTION]
1. Sign up
2. Dashboard → API → Copy your API key
3. ⚠️ CHECK: Note the actual free token grant shown on your dashboard.
   It may be 1M tokens or 10M. This determines how long before nomic fallback activates.

[SAVE]
JINA_API_KEY=jina_...
JINA_FREE_TIER_TOKENS=<actual number shown — 1000000 or 10000000>

[VERIFY]
curl https://api.jina.ai/v1/embeddings \
  -H "Authorization: Bearer $JINA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"jina-embeddings-v3","input":["test"],"dimensions":768}' \
  | python3 -m json.tool | grep '"index"'
→ Should show "index": 0 (one embedding returned)
```

💡 The `dimensions: 768` parameter is Jina v3's Matryoshka truncation — this is the locked decision (L17). Always pass this parameter. Never use the default 1024.

---

### 2.7 — Vercel

```
[WHERE] Browser → https://vercel.com
[ACTION]
1. Sign up with GitHub
2. No project to create yet — we'll connect the repo in Part 6
3. Note your Vercel team/username for later

[SAVE]
VERCEL_TEAM_SLUG=<your username or team slug>

[VERIFY] Can log in at vercel.com/dashboard
```

---

### 2.8 — Sentry

```
[WHERE] Browser → https://sentry.io
[ACTION]
1. Sign up with GitHub
2. Create Organization: alphalens
3. Create Project:
   Platform: Next.js
   Project name: alphalens-frontend
   Alert threshold: 10 errors/minute (default is fine)
4. Copy the DSN from the setup page

[SAVE]
SENTRY_DSN=https://xxx@xxx.ingest.sentry.io/xxx
SENTRY_ORG=alphalens
SENTRY_PROJECT=alphalens-frontend

[VERIFY] Project appears in Sentry dashboard
```

---

### 2.9 — Opik (LLM Tracing)

```
[WHERE] Browser → https://comet.com/opik (or https://app.opik.com)
[ACTION]
1. Sign up
2. Create workspace: alphalens
3. Settings → API Keys → Create key

[SAVE]
OPIK_API_KEY=<key>
OPIK_WORKSPACE=alphalens

[VERIFY] Can access workspace dashboard
```

---

### 2.10 — GitHub Repository

```
[WHERE] Browser → https://github.com
[ACTION]
1. Create new repository:
   Name: alphalens
   Visibility: Public (portfolio project)
   Initialize with README: YES
   .gitignore: Python
   License: MIT

[SAVE]
GITHUB_REPO_URL=https://github.com/<username>/alphalens.git

[WHERE] WSL terminal
[CMD]
cd ~
git clone $GITHUB_REPO_URL
cd alphalens

[VERIFY] ls → shows README.md, .gitignore, LICENSE
```

---

### ✅ Part 2 Done

You should have these saved:
```
NEON_DATABASE_URL
NEON_DIRECT_URL
R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_ENDPOINT_URL
GROQ_API_KEY
JINA_API_KEY
SENTRY_DSN, SENTRY_ORG, SENTRY_PROJECT
OPIK_API_KEY, OPIK_WORKSPACE
GITHUB_REPO_URL
```

---

## Part 3 — AWS Billing Guardrails

⚠️ **Do this BEFORE any AWS deploys.** A runaway Lambda loop can burn $50 in hours. These 5 layers are your protection.

All billing operations happen in `us-east-1` (AWS bills from there regardless of your app region).

---

### Layer 1 — AWS Free Tier Alerts

```
[WHERE] AWS Console → Billing → Billing Preferences
[ACTION]
1. Tick: "Receive Free Tier Usage Alerts"
2. Email: your email
3. Save preferences

[VERIFY] Confirmation email arrives
```

---

### Layer 2 — Zero-Spend Budget

```
[WHERE] AWS Console → Billing → Budgets → Create Budget
[ACTION]
Budget type: Cost Budget
Budget amount: $0.01
Scope: All AWS services
Alert threshold: 100% of budgeted amount (actual)
Email: your email
Name: alphalens-zero-spend

[VERIFY] Budget appears in Budgets list
```

---

### Layer 3 — Cost Budget with Tiered Alerts

```
[WHERE] AWS Console → Billing → Budgets → Create Budget
[ACTION]
Budget type: Cost Budget
Budget amount: $1.00
Name: alphalens-cost-guardrail
Alerts (add three):
  Alert 1: 50% actual   → email
  Alert 2: 80% forecast → email
  Alert 3: 100% actual  → email

[VERIFY] Budget appears with 3 alert thresholds
```

---

### Layer 4 — CloudWatch Billing Alarm

⚠️ CloudWatch billing metrics are ONLY in `us-east-1`. Switch region before doing this step.

```
[WHERE] AWS Console → CloudWatch (region: us-east-1) → Alarms → Create Alarm
[ACTION]
1. Select metric: Billing → Total Estimated Charge → USD
2. Threshold: Greater than $0.01
3. Alarm name: alphalens-billing-alarm
4. Action: Send notification → Create new SNS topic
   Topic name: alphalens-billing-sns
   Email: your email
5. Confirm SNS subscription in your email

[VERIFY]
Alarm state shows "OK" (not in alarm)
SNS subscription confirmed in email
```

---

### Layer 5 — Budget Action (Auto-Deny on Spend)

⚠️ This layer requires the IAM deployer user to exist. Come back to this step after Part 4.2.

```
[WHERE] AWS Console → Billing → Budgets → alphalens-cost-guardrail → Edit
[ACTION] Add Budget Action:
  Trigger: 100% actual spend
  Action type: IAM — Apply IAM policy
  Policy: AlphaLensEmergencyDeny (you'll create this in Part 4.3)
  Target: deployer IAM user
  Approval: Automatic (no manual approval needed)

[VERIFY] Action shows "Configured" status
```

---

### ✅ Part 3 Done (Layer 5 pending — complete after Part 4)

---

## Part 4 — AWS IAM Setup

💡 **Root account = never use for CLI.** You create two IAM users: `alphalens-admin` (for console ops) and `alphalens-deployer` (for CI/CD). One more policy `AlphaLensEmergencyDeny` acts as the kill switch.

---

### 4.1 — Create IAM Admin User

```
[WHERE] AWS Console → IAM → Users → Create User
[ACTION]
Username: alphalens-admin
Access type: AWS Management Console access (tick)
Password: set a strong password
MFA: Enable TOTP MFA after creation

Permissions: Attach policies directly
  - AdministratorAccess

[SAVE]
IAM_ADMIN_USER=alphalens-admin
```

From now on, log into AWS Console as `alphalens-admin`, not root.

---

### 4.2 — Create IAM Deployer User (for CLI + GitHub Actions)

```
[WHERE] AWS Console → IAM → Users → Create User
[ACTION]
Username: alphalens-deployer
Access type: Programmatic access (Access Key)

Permissions: Attach policies directly (create inline policy):
```

Create a custom policy named `AlphaLensDeployerPolicy`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ECRAccess",
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage",
        "ecr:DescribeRepositories",
        "ecr:CreateRepository",
        "ecr:PutLifecyclePolicy"
      ],
      "Resource": "*"
    },
    {
      "Sid": "LambdaAccess",
      "Effect": "Allow",
      "Action": [
        "lambda:CreateFunction",
        "lambda:UpdateFunctionCode",
        "lambda:UpdateFunctionConfiguration",
        "lambda:GetFunction",
        "lambda:GetFunctionConfiguration",
        "lambda:AddPermission",
        "lambda:CreateFunctionUrlConfig",
        "lambda:UpdateFunctionUrlConfig",
        "lambda:GetFunctionUrlConfig",
        "lambda:InvokeFunction",
        "lambda:ListFunctions"
      ],
      "Resource": "*"
    },
    {
      "Sid": "IAMPassRole",
      "Effect": "Allow",
      "Action": ["iam:PassRole", "iam:GetRole"],
      "Resource": "arn:aws:iam::*:role/alphalens-*"
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:PutRetentionPolicy",
        "logs:DescribeLogGroups"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CloudWatchAlarms",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:PutMetricAlarm",
        "cloudwatch:DescribeAlarms"
      ],
      "Resource": "*"
    }
  ]
}
```

```
[SAVE] After creating user, download/copy:
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=ap-southeast-1

[VERIFY]
aws configure --profile alphalens-deployer
# Enter the access key, secret key, region ap-southeast-1, output json

aws sts get-caller-identity --profile alphalens-deployer
→ Shows Account ID and ARN for alphalens-deployer
```

---

### 4.3 — Create Emergency Deny Policy

```
[WHERE] AWS Console → IAM → Policies → Create Policy
[ACTION]
Policy name: AlphaLensEmergencyDeny
JSON:
```

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyAllActions",
      "Effect": "Deny",
      "Action": "*",
      "Resource": "*"
    }
  ]
}
```

💡 **How this works:** When Layer 5 of billing (Part 3) triggers, AWS automatically attaches this policy to the `alphalens-deployer` user. A Deny always overrides Allow — so the deployer is instantly locked out from ALL actions, stopping any runaway deploys from burning more credits.

---

### 4.4 — Create Lambda Execution Role

Lambda needs an IAM role (not a user) to run and write logs.

```
[WHERE] AWS Console → IAM → Roles → Create Role
[ACTION]
Trusted entity: AWS Service → Lambda
Role name: alphalens-lambda-role

Attach policies:
  - AWSLambdaBasicExecutionRole (CloudWatch logs)

Add inline policy named AlphaLensLambdaInline:
```

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudWatchPutMetrics",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:PutMetricData"
      ],
      "Resource": "*"
    }
  ]
}
```

```
[SAVE]
LAMBDA_ROLE_ARN=arn:aws:iam::<ACCOUNT_ID>:role/alphalens-lambda-role

[VERIFY]
aws iam get-role --role-name alphalens-lambda-role \
  --profile alphalens-deployer
→ Shows role ARN
```

---

### 4.5 — Complete Layer 5 (Billing Action)

Now go back to Part 3 → Layer 5 and complete the Budget Action. The `AlphaLensEmergencyDeny` policy and `alphalens-deployer` user now exist.

---

### ✅ Part 4 Done

---

# DAY 2

---

## Part 5 — Repository Bootstrap

This is where the actual project structure gets created.

---

### 5.1 — Directory Structure

```
[WHERE] WSL terminal, inside ~/alphalens
[CMD]
mkdir -p \
  src/alphalens/{agent,etl,api,db} \
  docs/{design,specs,setup} \
  scripts \
  tests/{unit,integration} \
  infra \
  frontend

touch \
  src/alphalens/__init__.py \
  src/alphalens/agent/__init__.py \
  src/alphalens/etl/__init__.py \
  src/alphalens/api/__init__.py \
  src/alphalens/db/__init__.py \
  tests/__init__.py \
  tests/unit/__init__.py \
  tests/integration/__init__.py

[VERIFY] tree src/ (install tree: sudo apt install tree)
```

Expected structure:
```
alphalens/
├── src/
│   └── alphalens/
│       ├── agent/      # LangGraph nodes, state, circuit breaker
│       ├── etl/        # EDGAR client, chunker, embeddings, upsert
│       ├── api/        # FastAPI app, SSE endpoint, OIDC middleware
│       └── db/         # Schema, migrations, connection
├── docs/
│   ├── design/         # AlphaLens_v8.md lives here
│   ├── specs/          # 01_settings.md ... 17_frontend.md
│   └── setup/          # This guide lives here
├── scripts/            # smoke_test.py, seed_companies.py, etc.
├── tests/
│   ├── unit/
│   └── integration/
├── infra/              # Dockerfile, deploy scripts
├── frontend/           # Next.js app (initialized in Part 6.5)
├── pyproject.toml
├── .env.example
├── .env                # NEVER COMMIT THIS
├── .gitignore
└── README.md
```

---

### 5.2 — pyproject.toml

```
[WHERE] WSL terminal, ~/alphalens
[CMD] Create pyproject.toml with this content:
```

```toml
[project]
name = "alphalens"
version = "1.0.0"
description = "RAG agent over SEC 10-K/10-Q filings"
requires-python = ">=3.12,<3.13"
dependencies = [
    # API framework
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.30.0",
    "sse-starlette>=2.1.0",

    # LangGraph agent
    "langgraph>=0.2.0",
    "langchain-groq>=0.1.0",

    # Database
    "asyncpg>=0.29.0",
    "pgvector>=0.3.0",
    "alembic>=1.13.0",
    "sqlalchemy>=2.0.0",

    # Embeddings + reranker
    "httpx>=0.27.0",
    "sentence-transformers>=3.0.0",
    "torch>=2.3.0",

    # ETL
    "beautifulsoup4>=4.12.0",
    "lxml>=5.2.0",
    "spacy>=3.7.0",
    "boto3>=1.34.0",            # R2 S3-compat client

    # Auth
    "pyjwt[crypto]>=2.8.0",

    # Observability
    "opik>=1.0.0",
    "sentry-sdk[fastapi]>=2.0.0",

    # Config
    "pydantic-settings>=2.3.0",
    "python-dotenv>=1.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
    "ruff>=0.5.0",
    "mypy>=1.10.0",
    "httpx>=0.27.0",    # for FastAPI TestClient async
    "pre-commit>=3.7.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]
ignore = ["E501"]  # line length handled by formatter

[tool.ruff.format]
quote-style = "double"

[tool.mypy]
python_version = "3.12"
strict = true
ignore_missing_imports = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.hatch.build.targets.wheel]
packages = ["src/alphalens"]
```

```
[CMD] uv sync
[VERIFY] .venv/ directory created, uv.lock generated, no errors
```

---

### 5.3 — .env File

```
[WHERE] WSL terminal, ~/alphalens
[CMD] Create .env (NEVER commit this file)
```

```bash
# ── Database ──────────────────────────────────────────────────
NEON_DATABASE_URL=postgresql://user:pass@ep-xxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
NEON_DIRECT_URL=postgresql://user:pass@ep-xxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&endpoint=ep-xxx

# ── Cloudflare R2 ─────────────────────────────────────────────
R2_ACCOUNT_ID=your_cf_account_id
R2_ACCESS_KEY_ID=your_r2_access_key
R2_SECRET_ACCESS_KEY=your_r2_secret_key
R2_BUCKET_NAME=alphalens-filings
R2_ENDPOINT_URL=https://your_cf_account_id.r2.cloudflarestorage.com

# ── LLM ───────────────────────────────────────────────────────
GROQ_API_KEY=gsk_...
GROQ_MODEL=llama-3.3-70b-versatile

# ── Embeddings ────────────────────────────────────────────────
JINA_API_KEY=jina_...
JINA_MODEL=jina-embeddings-v3
JINA_DIMENSIONS=768
JINA_FREE_TIER_TOKENS=1000000   # update after checking your dashboard

# ── Reranker (in-process) ─────────────────────────────────────
RERANKER_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2

# ── AWS ───────────────────────────────────────────────────────
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=ap-southeast-1
LAMBDA_ROLE_ARN=arn:aws:iam::ACCOUNT_ID:role/alphalens-lambda-role
ECR_REGISTRY=ACCOUNT_ID.dkr.ecr.ap-southeast-1.amazonaws.com
ECR_REPO_NAME=alphalens-agent

# ── Observability ─────────────────────────────────────────────
SENTRY_DSN=https://xxx@xxx.ingest.sentry.io/xxx
OPIK_API_KEY=...
OPIK_WORKSPACE=alphalens

# ── App ───────────────────────────────────────────────────────
ENVIRONMENT=development     # development | production
LOG_LEVEL=INFO
```

```
[VERIFY] cat .env | head -5 → shows your DB URL (don't paste output anywhere)
```

---

### 5.4 — .env.example (Safe to Commit)

```
[CMD] Copy .env to .env.example and blank out all values:
cp .env .env.example
# Then manually replace all values with placeholder descriptions
```

```bash
# ── Database ──────────────────────────────────────────────────
NEON_DATABASE_URL=postgresql://USER:PASS@HOST/neondb?sslmode=require
NEON_DIRECT_URL=postgresql://USER:PASS@HOST/neondb?sslmode=require&endpoint=ENDPOINT_ID
# ... etc with placeholder values
```

---

### 5.5 — Ruff + pre-commit

```
[WHERE] WSL terminal, ~/alphalens
[CMD]
uv add --dev pre-commit
uv run pre-commit install
```

Create `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.5.0
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format

  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.18.4
    hooks:
      - id: gitleaks
        name: Detect secrets (gitleaks)

  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.6.0
    hooks:
      - id: check-large-files
        args: [--maxkb=500]
      - id: check-added-large-files
      - id: check-merge-conflict
      - id: end-of-file-fixer
      - id: trailing-whitespace
```

```
[CMD] uv run pre-commit run --all-files
[VERIFY] All hooks pass (or show fixable lint warnings that get auto-fixed)
```

---

### 5.6 — Update .gitignore

Add to `.gitignore`:

```
# Secrets — never commit
.env
*.env.local

# Python
__pycache__/
*.pyc
.venv/
*.egg-info/
dist/
.mypy_cache/
.ruff_cache/
.pytest_cache/

# Models (large binary files)
models/
*.bin
*.safetensors

# Docker
.docker/

# Node
frontend/node_modules/
frontend/.next/
frontend/.vercel/

# OS
.DS_Store
Thumbs.db
```

---

### 5.7 — Copy Design Doc and This Guide

```
[CMD]
cp ~/AlphaLens_v8.md docs/design/AlphaLens_v8.md
cp ~/AlphaLens_Phase1_Setup_Guide.md docs/setup/Phase1_Setup_Guide.md
mkdir -p docs/specs
```

---

### 5.8 — Initial Commit

```
[CMD]
git add .
git commit -m "chore: project scaffold, pyproject, pre-commit, env example"
git push origin main

[VERIFY]
GitHub repo shows the directory structure
.env is NOT in the commit (check: git show HEAD --name-only | grep .env → nothing)
```

---

### ✅ Part 5 Done

---

## Part 6 — Service Initialization

---

### 6.1 — Database Schema (Alembic + pgvector)

Create the Alembic baseline migration:

```
[CMD]
uv run alembic init src/alphalens/db/migrations
```

Edit `alembic.ini` — find `sqlalchemy.url` and set:

```ini
sqlalchemy.url = %(NEON_DIRECT_URL)s
```

Edit `src/alphalens/db/migrations/env.py` — add at top:

```python
from dotenv import load_dotenv
load_dotenv()
```

Create first migration `scripts/create_schema.sql`:

```sql
-- Enable pgvector
CREATE EXTENSION IF NOT EXISTS vector;

-- companies
CREATE TABLE IF NOT EXISTS companies (
    cik VARCHAR(10) PRIMARY KEY,
    ticker VARCHAR(10) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    sic_code VARCHAR(4),
    fiscal_year_end VARCHAR(4),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- filings
CREATE TABLE IF NOT EXISTS filings (
    id BIGSERIAL PRIMARY KEY,
    cik VARCHAR(10) REFERENCES companies(cik),
    accession_number VARCHAR(20) UNIQUE NOT NULL,
    form_type VARCHAR(10) NOT NULL,
    filing_date DATE NOT NULL,
    period_of_report DATE NOT NULL,
    primary_doc_url TEXT NOT NULL,
    r2_html_key TEXT,
    state VARCHAR(20) NOT NULL DEFAULT 'pending',
    state_updated_at TIMESTAMPTZ DEFAULT now(),
    error_message TEXT,
    retry_count INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_filings_cik_period ON filings(cik, period_of_report DESC);
CREATE INDEX IF NOT EXISTS idx_filings_state ON filings(state) WHERE state != 'indexed';

-- chunks (768d vector — locked L17)
CREATE TABLE IF NOT EXISTS chunks (
    id BIGSERIAL PRIMARY KEY,
    filing_id BIGINT REFERENCES filings(id) ON DELETE CASCADE,
    section VARCHAR(100) NOT NULL,
    section_order INT NOT NULL,
    chunk_index INT NOT NULL,
    text TEXT NOT NULL,
    token_count INT NOT NULL,
    embedding VECTOR(768),
    embedding_model_version VARCHAR(50) NOT NULL,
    tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw ON chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_chunks_filing_section ON chunks(filing_id, section);

-- financial_facts + entities: deferred to v2 (XBRL / Apache AGE KG); not created in v1.
```

Run the schema:

```
[CMD]
source .env
psql "$NEON_DIRECT_URL" -f scripts/create_schema.sql

[VERIFY]
psql "$NEON_DIRECT_URL" -c "\dt"
→ Shows: companies, filings, chunks, ingestion_jobs, queries

psql "$NEON_DIRECT_URL" -c "\di" | grep hnsw
→ Shows: idx_chunks_embedding_hnsw
```

---

### 6.2 — Seed Companies Table

Create `scripts/seed_companies.py`:

```python
"""
Seed the companies table with top 10 S&P 500 companies.
Run once: uv run python scripts/seed_companies.py
"""

import asyncio
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()

COMPANIES = [
    ("0000320193", "AAPL", "Apple Inc.", "3674", "0930"),
    ("0000789019", "MSFT", "Microsoft Corporation", "7372", "0630"),
    ("0001652044", "GOOGL", "Alphabet Inc.", "7370", "1231"),
    ("0001018724", "AMZN", "Amazon.com Inc.", "5961", "1231"),
    ("0001326801", "META", "Meta Platforms Inc.", "7370", "1231"),
    ("0001045810", "NVDA", "NVIDIA Corporation", "3674", "0126"),
    ("0001067983", "BRK-B", "Berkshire Hathaway Inc.", "6331", "1231"),
    ("0000051143", "JPM", "JPMorgan Chase & Co.", "6022", "1231"),
    ("0000078003", "JNJ", "Johnson & Johnson", "2836", "1231"),
    ("0000200406", "V", "Visa Inc.", "7389", "0930"),
]


async def seed():
    conn = await asyncpg.connect(os.environ["NEON_DIRECT_URL"])
    try:
        for cik, ticker, name, sic, fye in COMPANIES:
            await conn.execute(
                """
                INSERT INTO companies (cik, ticker, name, sic_code, fiscal_year_end)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (cik) DO NOTHING
                """,
                cik, ticker, name, sic, fye,
            )
        count = await conn.fetchval("SELECT COUNT(*) FROM companies")
        print(f"✅ Companies seeded: {count} rows")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(seed())
```

```
[CMD] uv run python scripts/seed_companies.py
[VERIFY] Output: ✅ Companies seeded: 10 rows
```

---

### 6.3 — R2 Lifecycle Policy

```
[WHERE] Cloudflare Dashboard → R2 → alphalens-filings → Settings → Lifecycle Rules
[ACTION]
Add rule:
  Rule name: expire-temp-files
  Prefix: tmp/
  Action: Delete after 7 days

[VERIFY] Rule appears in lifecycle rules list
```

---

### 6.4 — ECR Repository

```
[WHERE] WSL terminal
[CMD]
# Create single ECR repo (reranker merged into agent — one container)
aws ecr create-repository \
  --repository-name alphalens-agent \
  --region ap-southeast-1 \
  --profile alphalens-deployer

# Set lifecycle policy: keep only last 3 images (saves ECR storage cost)
aws ecr put-lifecycle-policy \
  --repository-name alphalens-agent \
  --region ap-southeast-1 \
  --profile alphalens-deployer \
  --lifecycle-policy-text '{
    "rules": [{
      "rulePriority": 1,
      "description": "Keep last 3 images",
      "selection": {
        "tagStatus": "any",
        "countType": "imageCountMoreThan",
        "countNumber": 3
      },
      "action": {"type": "expire"}
    }]
  }'

[VERIFY]
aws ecr describe-repositories \
  --region ap-southeast-1 \
  --profile alphalens-deployer
→ Shows alphalens-agent repository
```

---

### 6.5 — Initialize Next.js Frontend

```
[WHERE] WSL terminal, ~/alphalens
[CMD]
cd frontend
npx create-next-app@latest . \
  --typescript \
  --tailwind \
  --eslint \
  --app \
  --no-src-dir \
  --import-alias "@/*"

[VERIFY]
npm run dev → http://localhost:3000 loads Next.js default page
Ctrl+C to stop
```

---

### 6.6 — Set Log Retention on CloudWatch

⚠️ CloudWatch Logs default = infinite retention = real cost. Set 7-day retention now, before any Lambda is deployed.

```
[CMD]
# This script runs after Lambda is deployed, but create it now
cat > scripts/set_log_retention.sh << 'EOF'
#!/bin/bash
# Run after each Lambda deploy to ensure log retention is capped
aws logs put-retention-policy \
  --log-group-name /aws/lambda/alphalens-agent \
  --retention-in-days 7 \
  --region ap-southeast-1 \
  --profile alphalens-deployer
echo "✅ Log retention set to 7 days"
EOF
chmod +x scripts/set_log_retention.sh
```

---

### 6.7 — Commit Service Init

```
[CMD]
git add .
git commit -m "chore: schema, seed script, ECR repo, frontend scaffold"
git push origin main
```

---

### ✅ Part 6 Done

---

## Part 7 — Smoke Test

The smoke test verifies all 5 external connections work before Phase 2 starts. **Do not begin Phase 2 if any check fails.**

Create `scripts/smoke_test.py`:

```python
"""
AlphaLens Phase 1 Smoke Test
Run: uv run python scripts/smoke_test.py
All 5 checks must pass before Phase 2 begins.
"""

import asyncio
import os
import sys
import httpx
import asyncpg
import boto3
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv()

PASS = "✅"
FAIL = "❌"
results: list[tuple[str, bool, str]] = []


async def check_neon() -> None:
    """Check 1: Neon Postgres — pooled connection + pgvector + schema"""
    name = "Neon Postgres"
    try:
        conn = await asyncpg.connect(os.environ["NEON_DATABASE_URL"])
        try:
            ext = await conn.fetchval(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            )
            assert ext is not None, "pgvector extension not installed"

            tables = await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
            table_names = {r["tablename"] for r in tables}
            required = {"companies", "filings", "chunks", "ingestion_jobs", "queries"}
            missing = required - table_names
            assert not missing, f"Missing tables: {missing}"

            count = await conn.fetchval("SELECT COUNT(*) FROM companies")
            assert count == 10, f"Expected 10 companies, got {count}"

            idx = await conn.fetchval(
                "SELECT indexname FROM pg_indexes WHERE indexname = 'idx_chunks_embedding_hnsw'"
            )
            assert idx is not None, "HNSW index missing"

            results.append((name, True, f"pgvector {ext}, 5 tables, 10 companies, HNSW OK"))
        finally:
            await conn.close()
    except Exception as e:
        results.append((name, False, str(e)))


async def check_r2() -> None:
    """Check 2: Cloudflare R2 — bucket accessible"""
    name = "Cloudflare R2"
    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=os.environ["R2_ENDPOINT_URL"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )
        resp = s3.list_objects_v2(Bucket=os.environ["R2_BUCKET_NAME"])
        count = resp.get("KeyCount", 0)
        results.append((name, True, f"Bucket accessible, {count} objects"))
    except Exception as e:
        results.append((name, False, str(e)))


async def check_groq() -> None:
    """Check 3: Groq API — list models, verify llama-3.3-70b available"""
    name = "Groq API"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
            )
            resp.raise_for_status()
            models = [m["id"] for m in resp.json()["data"]]
            target = "llama-3.3-70b-versatile"
            assert target in models, f"{target} not found in Groq models"
            results.append((name, True, f"{target} available"))
    except Exception as e:
        results.append((name, False, str(e)))


async def check_jina() -> None:
    """Check 4: Jina API — embed one text at 768d, verify vector shape"""
    name = "Jina Embeddings"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.jina.ai/v1/embeddings",
                headers={
                    "Authorization": f"Bearer {os.environ['JINA_API_KEY']}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "jina-embeddings-v3",
                    "input": ["smoke test: Apple revenue 2024"],
                    "dimensions": 768,
                    "task": "retrieval.query",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            vec = data["data"][0]["embedding"]
            assert len(vec) == 768, f"Expected 768d, got {len(vec)}d"
            results.append((name, True, f"768d embedding returned, first val: {vec[0]:.4f}"))
    except Exception as e:
        results.append((name, False, str(e)))


async def check_aws_ecr() -> None:
    """Check 5: AWS ECR — deployer can describe the alphalens-agent repo"""
    name = "AWS ECR"
    try:
        ecr = boto3.client(
            "ecr",
            region_name="ap-southeast-1",
            aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        )
        repos = ecr.describe_repositories(repositoryNames=["alphalens-agent"])
        repo_uri = repos["repositories"][0]["repositoryUri"]
        results.append((name, True, f"Repo URI: {repo_uri}"))
    except Exception as e:
        results.append((name, False, str(e)))


async def main() -> None:
    print("\n🔍 AlphaLens Phase 1 Smoke Test\n" + "=" * 40)

    await asyncio.gather(
        check_neon(),
        check_r2(),
        check_groq(),
        check_jina(),
        check_aws_ecr(),
    )

    print()
    all_pass = True
    for name, passed, detail in results:
        icon = PASS if passed else FAIL
        print(f"{icon} {name}")
        print(f"   {detail}")
        if not passed:
            all_pass = False

    print("\n" + "=" * 40)
    if all_pass:
        print("✅ ALL 5 CHECKS PASSED — Phase 2 can begin!")
    else:
        failed = sum(1 for _, p, _ in results if not p)
        print(f"❌ {failed}/5 checks failed. Fix before proceeding to Phase 2.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
```

```
[CMD] uv run python scripts/smoke_test.py

[VERIFY]
✅ Neon Postgres
✅ Cloudflare R2
✅ Groq API
✅ Jina Embeddings
✅ AWS ECR
✅ ALL 5 CHECKS PASSED — Phase 2 can begin!
```

---

## Part 8 — Definition of Done

Phase 1 is complete when ALL of the following are true:

### Environment ✅
- [ ] Python 3.12, uv, Node 20, AWS CLI v2, Docker all verified in WSL
- [ ] VS Code + WSL extension working
- [ ] pre-commit hooks installed and passing

### Services ✅
- [ ] Neon project created (ap-southeast-1), pgvector enabled
- [ ] R2 bucket `alphalens-filings` created with lifecycle rule
- [ ] Groq API key working, llama-3.3-70b-versatile available
- [ ] Jina API key working, 768d embeddings confirmed
- [ ] Vercel account created
- [ ] Sentry project created
- [ ] Opik workspace created
- [ ] GitHub repo `alphalens` created and cloned

### AWS ✅
- [ ] 5-layer billing guardrails active (all 5 layers done including Layer 5 Budget Action)
- [ ] IAM users: `alphalens-admin` (console), `alphalens-deployer` (CLI/CI)
- [ ] Lambda role `alphalens-lambda-role` created
- [ ] Emergency deny policy `AlphaLensEmergencyDeny` created and wired to Layer 5
- [ ] ECR repo `alphalens-agent` created with 3-image lifecycle policy

### Repo ✅
- [ ] Directory structure scaffolded (src/alphalens/{agent,etl,api,db}, docs/specs, etc.)
- [ ] `pyproject.toml` with all dependencies
- [ ] `.env` populated with all API keys (NOT committed)
- [ ] `.env.example` committed with placeholder values
- [ ] pre-commit config committed and hooks installed
- [ ] Design doc `AlphaLens_v8.md` in `docs/design/`
- [ ] This setup guide in `docs/setup/`

### Database ✅
- [ ] 5 tables created: companies, filings, chunks, ingestion_jobs, queries
- [ ] pgvector extension enabled (`vector` extension version 0.7+)
- [ ] HNSW index on `chunks.embedding VECTOR(768)` confirmed
- [ ] GIN index on `chunks.tsv` confirmed
- [ ] 10 companies seeded in `companies` table

### Smoke Test ✅
- [ ] `scripts/smoke_test.py` runs and shows 5/5 checks passing

---

## Part 9 — What's Next: Phase 2

Once Phase 1 is complete, Phase 2 begins immediately.

### Phase 2 Flow

```
Step 1 — Author all 17 specs in docs/specs/
          Format: Goal + Function Signatures + Acceptance Criteria + Gotchas
          ~1 page each. 17 specs total.

Step 2 — Implement in order with Claude Code:

  Phase 1.A — Core Pipeline (specs 01-08)
  ┌─────────────────────────────────────────────────────────┐
  │ 01_settings.md         → src/alphalens/config.py        │
  │ 02_db_schema.md        → src/alphalens/db/schema.py     │
  │ 03_edgar_client.md     → src/alphalens/etl/edgar.py     │
  │ 04_section_detector.md → src/alphalens/etl/sections.py  │
  │ 05_chunker.md          → src/alphalens/etl/chunker.py   │
  │ 06_embedding_client.md → src/alphalens/etl/embeddings.py│  ← Jina + nomic built here
  │ 07_upsert_pipeline.md  → src/alphalens/etl/upsert.py   │
  │ 08_filing_state.md     → src/alphalens/etl/state.py     │
  └─────────────────────────────────────────────────────────┘

  Phase 1.B — Agent (specs 09-12)
  ┌─────────────────────────────────────────────────────────┐
  │ 09_agent_state.md      → src/alphalens/agent/state.py   │
  │ 10_agent_nodes.md      → src/alphalens/agent/nodes.py   │  ← 5 nodes
  │ 11_circuit_breaker.md  → src/alphalens/agent/breaker.py │
  │ 12_retrieval.md        → src/alphalens/agent/retrieval.py│
  └─────────────────────────────────────────────────────────┘

  Phase 1.C — Deployment (specs 13-17)
  ┌─────────────────────────────────────────────────────────┐
  │ 13_dockerfile.md       → infra/Dockerfile               │  ← single container
  │ 14_lambda_deploy.md    → infra/deploy.sh                │  ← OIDC middleware
  │ 15_r2_setup.md         → scripts/r2_init.py             │
  │ 16_observability.md    → src/alphalens/api/telemetry.py │
  │ 17_frontend.md         → frontend/ (Next.js)            │
  └─────────────────────────────────────────────────────────┘

Step 3 — ETL run: ingest ~200 filings → state='indexed' for ≥95%
Step 4 — Agent test: 10 golden queries return cited answers
Step 5 — Deploy: Lambda live + Vercel live + smoke test passes in production
```

---

## Appendix — Troubleshooting Common Issues

### "Connection refused" on Neon
- Use the pooled URL for app, direct URL for psql/migrations. They are different.
- Neon free tier auto-suspends after 5 min idle. First connection after sleep takes ~2s. Normal.

### Jina returning 1024d instead of 768d
- You forgot `"dimensions": 768` in the request body. Always pass this parameter explicitly.
- The default Jina v3 output is 1024. Our locked decision (L17) requires truncation.

### Docker: permission denied on /var/run/docker.sock
```
sudo usermod -aG docker $USER
# Log out and back into WSL
```

### AWS CLI "Unable to locate credentials"
- Make sure you run `aws configure --profile alphalens-deployer` and use `--profile alphalens-deployer` in all commands.
- Or set `AWS_PROFILE=alphalens-deployer` in your `.env`.

### pre-commit "gitleaks" fails on .env
- This is correct behavior — gitleaks found a secret. Make sure `.env` is in `.gitignore` and NOT staged.
- `git rm --cached .env` if accidentally staged.

### nomic-embed-text-v1.5 out of memory in Lambda
- nomic needs ~1.5–2GB RAM. Lambda memory must be set to at minimum 3GB.
- This is handled in spec 13 (Dockerfile) and spec 14 (Lambda config) — **both deferred to v2** per the 12 Aug 2026 amendment (v8 §13.4a). Not a v1 concern.

### uv.lock merge conflicts
- Run `uv sync` after resolving — uv regenerates the lockfile from pyproject.toml automatically.

---

**End of Phase 1 Setup Guide.**

When `scripts/smoke_test.py` shows 5/5 ✅, you are ready for Phase 2.
Spec authoring starts at `docs/specs/01_settings.md`.
