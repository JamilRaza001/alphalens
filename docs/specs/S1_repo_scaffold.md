Spec S1 — repo\_scaffold
------------------------

**Phase**: 1 (Setup)**Depends on**: Phase 1 Parts 1-4 complete (environment, services, billing, IAM)**Output**: Bootstrapped repository ready for code

### Goal

alphalens repository ko production-ready scaffold mein convert karna. Yeh Spec directory structure banata hai, Python project configuration (pyproject.toml with uv) set up karta hai, environment files (.env, .env.example) banata hai, pre-commit hooks install karta hai jo har commit pe lint + secret-scan + large-file check chalayein, design documentation docs/ mein copy karta hai, aur initial commit GitHub pe push karta hai. Iske baad code likhna shuru ho sakta hai — har subsequent Spec ke liye foundation ready hoga.

**Yeh Spec code MODULES nahi banati** — sirf \_\_init\_\_.py stubs banati hai taake Python ko package structure samajh aaye. Actual implementation Phase 2 ke Specs mein hogi.

### Working Directory

Sab kuch /mnt/c/MJR Work Space/AlphaLens/alphalens ke andar hona chahiye. Path mein space hai — har shell command mein quoted path use karna mandatory hai.

### Function Signatures

Yeh Spec code modules nahi banati, lekin yeh **files aur unka contract** banni chahiye:

#### File 1: pyproject.toml (root mein)

toml

Plain textANTLR4BashCC#CSSCoffeeScriptCMakeDartDjangoDockerEJSErlangGitGoGraphQLGroovyHTMLJavaJavaScriptJSONJSXKotlinLaTeXLessLuaMakefileMarkdownMATLABMarkupObjective-CPerlPHPPowerShell.propertiesProtocol BuffersPythonRRubySass (Sass)Sass (Scss)SchemeSQLShellSwiftSVGTSXTypeScriptWebAssemblyYAMLXML`   [project]  name = "alphalens"  version = "1.0.0"  description = "RAG agent over SEC 10-K/10-Q filings"  requires-python = ">=3.12,<3.13"  dependencies = [      # Exact list v8 Phase1_Setup_Guide §5.2 mein hai — usko 1:1 copy karo      # Specifically: fastapi, uvicorn[standard], sse-starlette, langgraph, langchain-groq,      # asyncpg, pgvector, alembic, sqlalchemy, httpx, sentence-transformers, torch,      # beautifulsoup4, lxml, spacy, boto3, pyjwt[crypto], opik, sentry-sdk[fastapi],      # pydantic-settings, python-dotenv  ]  [project.optional-dependencies]  dev = ["pytest>=8.0.0", "pytest-asyncio>=0.23.0", "ruff>=0.5.0", "mypy>=1.10.0", "httpx>=0.27.0", "pre-commit>=3.7.0"]  # Build, ruff, mypy, pytest, hatch sections — exact v8 guide §5.2 copy   `

#### File 2: .env (NEVER commit)

v8 Phase1\_Setup\_Guide §5.3 ka full template, but with **actual values** from Session 2 summary (Neon URLs, R2 creds, Groq, Jina, Sentry, Opik, AWS keys).

#### File 3: .env.example (commit safe)

.env ki copy, but har value placeholder string se replace. Format example:

bash

Plain textANTLR4BashCC#CSSCoffeeScriptCMakeDartDjangoDockerEJSErlangGitGoGraphQLGroovyHTMLJavaJavaScriptJSONJSXKotlinLaTeXLessLuaMakefileMarkdownMATLABMarkupObjective-CPerlPHPPowerShell.propertiesProtocol BuffersPythonRRubySass (Sass)Sass (Scss)SchemeSQLShellSwiftSVGTSXTypeScriptWebAssemblyYAMLXML`   NEON_DATABASE_URL=postgresql://USER:PASS@HOST.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require  GROQ_API_KEY=gsk_REPLACE_WITH_YOUR_KEY   `

#### File 4: .gitignore

v8 Phase1\_Setup\_Guide §5.6 ka full template — .env, \_\_pycache\_\_, .venv, models/, frontend/node\_modules/, etc.

#### File 5: .pre-commit-config.yaml

v8 Phase1\_Setup\_Guide §5.5 ka exact template — ruff (lint + format), gitleaks (secret scan), pre-commit-hooks (large file, merge conflict, EOF, trailing whitespace).

#### Directory Structure (empty \_\_init\_\_.py files)

Plain textANTLR4BashCC#CSSCoffeeScriptCMakeDartDjangoDockerEJSErlangGitGoGraphQLGroovyHTMLJavaJavaScriptJSONJSXKotlinLaTeXLessLuaMakefileMarkdownMATLABMarkupObjective-CPerlPHPPowerShell.propertiesProtocol BuffersPythonRRubySass (Sass)Sass (Scss)SchemeSQLShellSwiftSVGTSXTypeScriptWebAssemblyYAMLXML`   src/alphalens/__init__.py  src/alphalens/agent/__init__.py  src/alphalens/etl/__init__.py  src/alphalens/api/__init__.py  src/alphalens/db/__init__.py  tests/__init__.py  tests/unit/__init__.py  tests/integration/__init__.py  docs/design/         (folder, no __init__.py — not Python)  docs/specs/          (folder)  docs/setup/          (folder)  scripts/             (folder)  infra/               (folder)  frontend/            (folder, will be initialized in S4 — leave empty for now)   `

#### File 6: docs/design/AlphaLens\_v8.md

v8 master document. Copy from wherever it currently lives.

#### File 7: docs/setup/Phase1\_Setup\_Guide.md

Phase 1 setup guide. Copy from wherever it currently lives.

#### File 8: docs/specs/S1\_repo\_scaffold.md

**Yeh Spec khud** save ho jaye (taake permanent record rahe).

#### File 9: README.md (update existing)

Existing README ko expand karo. Minimum content:

*   Project name + tagline (from v8 §2.1)

*   Stack summary (1 line from v8 Quick Summary)

*   Setup instructions (point to docs/setup/Phase1\_Setup\_Guide.md)

*   License (MIT — already in repo)

*   Status badge: "Phase 1 setup in progress"


### Acceptance Criteria

Yeh sab **testable** hain. Implementation tabhi "done" hai jab har point pass ho.

1.  **Directory structure exact match karta hai** — tree -L 3 -a -I 'node\_modules|.venv|.git|\_\_pycache\_\_' chalayein, output v8 Phase1\_Setup\_Guide §5.1 wale structure se match kare.

2.  **pyproject.toml valid hai** — uv sync bina error ke complete ho. .venv/ directory create ho. uv.lock file generate ho.

3.  **Python 3.12 pin hai** — uv run python --version output Python 3.12.x ho.

4.  **Sab dependencies install hain** — uv pip list mein yeh sab dikhe: fastapi, langgraph, asyncpg, pgvector, alembic, sentence-transformers, torch, pyjwt, opik, sentry-sdk, pydantic-settings.

5.  **Dev dependencies installed hain** — uv pip list mein: pytest, pytest-asyncio, ruff, mypy, pre-commit.

6.  **.env exists with real values** — file present ho /mnt/c/MJR Work Space/AlphaLens/alphalens/.env pe, sab variables filled hon Session 2 summary se.

7.  **.env git mein NAHI hai** — git check-ignore -v .env output mein .gitignore:... show kare.Aur double-check: git ls-files | grep -F .env output mein **sirf** .env.example aaye, .env **kabhi nahi**.

8.  **.env.example git mein HAI** — git ls-files | grep -F .env.example mein dikhe.

9.  **Pre-commit hooks installed hain** — .git/hooks/pre-commit file exist kare. uv run pre-commit run --all-files chale (warnings ok, but no fatal errors).

10.  **Gitleaks pass hota hai** — uv run pre-commit run gitleaks --all-files exit code 0 return kare (matlab .env secrets exposed nahi hain).

11.  **Ruff lint pass hota hai** — uv run ruff check . exit code 0. (Code abhi nahi hai toh trivially pass karega — yeh future-proofing hai.)

12.  **Design docs docs/ mein hain** —

    *   docs/design/AlphaLens\_v8.md exists, file size > 10 KB (matlab full copy hai, stub nahi)

    *   docs/setup/Phase1\_Setup\_Guide.md exists, file size > 10 KB

    *   docs/specs/S1\_repo\_scaffold.md exists (yeh Spec)

13.  **README.md updated hai** — cat README.md mein "AlphaLens" title, stack mention, aur setup guide ka link ho.

14.  **Initial commit done hai** — git log --oneline mein commit dikhe: chore: project scaffold, pyproject, pre-commit, env example.

15.  **GitHub pe push ho gayi hai** — GitHub repo URL pe browser mein jao, directory structure dikhna chahiye. Local mein verify: git status clean ho, git log origin/main..HEAD empty ho (matlab local aur remote sync hain).

16.  **.env accidentally GitHub pe NAHI gaya** — GitHub web UI pe jao, repo mein .env search karo — kuch nahi milna chahiye. Backup check: git show HEAD --name-only | grep -E '^\\.env$' empty return kare.


### Gotchas

1.  **Path mein space hai** — cd "/mnt/c/MJR Work Space/AlphaLens/alphalens" mein quotes mandatory hain. Bina quotes ke cd fail ho jayega. Har shell command mein quoted path use karo. Agar Claude Code script likhe jisme path hai, double-quotes use ho.

2.  **.env pehle commit ho jaye to disaster hai** — Workflow strict: pehle .gitignore create karo (with .env line), **fir** .env banao. Reverse order mein agar pehle .env create hua aur stage ho gaya, toh git rm --cached .env se cleanup karna padega. Best practice: git status har bade step ke baad chalao — agar .env "Untracked files" mein dikha aur .gitignore se exclude ho raha hai, toh "Untracked" se bhi gayab ho jana chahiye.

3.  **uv sync PyTorch download bohot bada hai** — torch>=2.3.0 ~800 MB CPU wheel hai. First uv sync mein 5-10 min lag sakte hain depending on Pakistan internet speed. Yeh normal hai, ghabrana nahi. Agar 30 min se zyada ho raha hai toh --verbose flag se debug karo.

4.  **Pre-commit ka gitleaks pehli baar download karega** — first uv run pre-commit run --all-files 1-2 min slow hoga kyunki hooks ke binaries pull ho rahe hain. Subsequent runs fast.

5.  **Windows line endings (CRLF) vs Unix (LF)** — Tum WSL mein hai but folder Windows filesystem (/mnt/c/) pe hai. Git pull/push mein CRLF/LF conflict ho sakta hai. Pre-commit ka end-of-file-fixer hook isko handle karta hai, but agar weird errors aayein toh git config --global core.autocrlf input set karo.

6.  **Permissions on /mnt/c/** — Windows filesystem WSL ke andar slightly slow hai aur permissions weird ho sakti hain. uv aur Docker thoda slow chalega yahan. Yeh acceptable hai abhi ke liye — performance optimization Phase 2 ka kaam hai agar zarurat ho.

7.  **.env.example ko .env se generate karo, ulta nahi** — Workflow: cp .env .env.example se start karo, **phir** sed/manual edit se sab real values ko placeholders se replace karo. Ulta nahi (placeholder file pehle banake real values bharo — error-prone hai).


### Verification Script (Optional but Recommended)

S1 implementation ke baad yeh script chalayein. Sab acceptance criteria ek shot mein check ho jayenge.

bash

Plain textANTLR4BashCC#CSSCoffeeScriptCMakeDartDjangoDockerEJSErlangGitGoGraphQLGroovyHTMLJavaJavaScriptJSONJSXKotlinLaTeXLessLuaMakefileMarkdownMATLABMarkupObjective-CPerlPHPPowerShell.propertiesProtocol BuffersPythonRRubySass (Sass)Sass (Scss)SchemeSQLShellSwiftSVGTSXTypeScriptWebAssemblyYAMLXML`   #!/bin/bash  # scripts/verify_s1.sh — Save this file as part of S1  set -e  cd "/mnt/c/MJR Work Space/AlphaLens/alphalens"  echo "▸ Checking directory structure..."  for dir in src/alphalens/agent src/alphalens/etl src/alphalens/api src/alphalens/db \             tests/unit tests/integration docs/design docs/specs docs/setup \             scripts infra frontend; do    [ -d "$dir" ] || { echo "❌ Missing: $dir"; exit 1; }  done  echo "✅ All directories present"  echo "▸ Checking critical files..."  for f in pyproject.toml .env .env.example .gitignore .pre-commit-config.yaml README.md \           docs/design/AlphaLens_v8.md docs/setup/Phase1_Setup_Guide.md \           docs/specs/S1_repo_scaffold.md; do    [ -f "$f" ] || { echo "❌ Missing: $f"; exit 1; }  done  echo "✅ All files present"  echo "▸ Checking .env is gitignored..."  git check-ignore .env > /dev/null && echo "✅ .env is gitignored" || { echo "❌ .env NOT gitignored"; exit 1; }  echo "▸ Checking .env NOT in git history..."  git ls-files | grep -E '^\.env$' && { echo "❌ .env is tracked!"; exit 1; } || echo "✅ .env not tracked"  echo "▸ Checking Python version..."  uv run python --version | grep -q "3.12" && echo "✅ Python 3.12" || { echo "❌ Wrong Python"; exit 1; }  echo "▸ Checking key dependencies..."  for pkg in fastapi langgraph asyncpg pgvector alembic; do    uv pip list 2>/dev/null | grep -qi "^$pkg " || { echo "❌ Missing dep: $pkg"; exit 1; }  done  echo "✅ Key dependencies installed"  echo "▸ Running pre-commit..."  uv run pre-commit run --all-files || echo "⚠️  Pre-commit warnings (review)"  echo ""  echo "═══════════════════════════════════════"  echo "✅ S1 VERIFICATION PASSED"  echo "═══════════════════════════════════════"   `

### Workflow (Tumhare liye)

1.  Yeh Spec copy karo

2.  File save karo docs/specs/S1\_repo\_scaffold.md mein (S1 ka hi part hai)

3.  Claude Code kholo (VS Code mein), Plan mode on karo

4.  Bolo: _"Implement docs/specs/S1\_repo\_scaffold.md. Follow all acceptance criteria. Stop after creating verification script — don't run it yet, I'll verify manually."_

5.  Claude Code plan dikhayega → review karo → approve

6.  Execution hone do

7.  Final approval: tum bash scripts/verify\_s1.sh chalao, output check karo

8.  Mujhe batao kya hua — pass/fail + koi unexpected behavior

9.  Phir S2 (db\_schema\_and\_seed) likhunga
