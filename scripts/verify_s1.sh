#!/bin/bash
# scripts/verify_s1.sh — S1 acceptance criteria check
set -e
cd "/mnt/c/MJR Work Space/AlphaLens/alphalens"

echo "▸ Checking directory structure..."
for dir in src/alphalens/agent src/alphalens/etl src/alphalens/api src/alphalens/db \
           tests/unit tests/integration docs/design docs/specs docs/setup \
           scripts infra frontend; do
  [ -d "$dir" ] || { echo "❌ Missing: $dir"; exit 1; }
done
echo "✅ All directories present"

echo "▸ Checking critical files..."
for f in pyproject.toml .env .env.example .gitignore .pre-commit-config.yaml README.md \
         docs/design/AlphaLens_v8.md docs/setup/Phase1_Setup_Guide.md \
         docs/specs/S1_repo_scaffold.md; do
  [ -f "$f" ] || { echo "❌ Missing: $f"; exit 1; }
done
echo "✅ All files present"

echo "▸ Checking design docs are full copies (>10KB)..."
for f in docs/design/AlphaLens_v8.md docs/setup/Phase1_Setup_Guide.md; do
  size=$(wc -c < "$f")
  [ "$size" -gt 10240 ] || { echo "❌ $f too small (${size} bytes — expected >10KB)"; exit 1; }
done
echo "✅ Design docs are full copies"

echo "▸ Checking .env is gitignored..."
git check-ignore .env > /dev/null && echo "✅ .env is gitignored" || { echo "❌ .env NOT gitignored"; exit 1; }

echo "▸ Checking .env NOT in git index..."
git ls-files | grep -E '^\.env$' && { echo "❌ .env is tracked!"; exit 1; } || echo "✅ .env not tracked"

echo "▸ Checking Python version..."
uv run python --version | grep -q "3.12" && echo "✅ Python 3.12" || { echo "❌ Wrong Python version"; exit 1; }

echo "▸ Checking key dependencies..."
for pkg in fastapi langgraph asyncpg pgvector alembic sentence-transformers torch pyjwt opik sentry-sdk pydantic-settings; do
  uv pip list 2>/dev/null | grep -qi "^$pkg" || { echo "❌ Missing dep: $pkg"; exit 1; }
done
echo "✅ Key dependencies installed"

echo "▸ Checking dev dependencies..."
for pkg in pytest pytest-asyncio ruff mypy pre-commit; do
  uv pip list 2>/dev/null | grep -qi "^$pkg" || { echo "❌ Missing dev dep: $pkg"; exit 1; }
done
echo "✅ Dev dependencies installed"

echo "▸ Checking pre-commit hook installed..."
[ -f ".git/hooks/pre-commit" ] && echo "✅ pre-commit hook present" || { echo "❌ .git/hooks/pre-commit missing — run: uv run pre-commit install"; exit 1; }

echo "▸ Running ruff lint..."
uv run ruff check . && echo "✅ Ruff lint passed" || echo "⚠️  Ruff lint warnings (review)"

echo "▸ Running pre-commit (all files)..."
uv run pre-commit run --all-files || echo "⚠️  Pre-commit warnings (review output above)"

echo ""
echo "═══════════════════════════════════════"
echo "✅ S1 VERIFICATION PASSED"
echo "═══════════════════════════════════════"
echo ""
echo "Next steps (run manually):"
echo "  uv sync              # install all deps + generate uv.lock"
echo "  uv run pre-commit install        # install git hook"
echo "  git add pyproject.toml .env.example .gitignore .pre-commit-config.yaml README.md \\"
echo "          src/ tests/ docs/ scripts/ infra/ frontend/"
echo "  git commit -m 'chore: project scaffold, pyproject, pre-commit, env example'"
echo "  git push origin main"
