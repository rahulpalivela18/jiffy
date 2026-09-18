#!/usr/bin/env bash
# One-command start for jiffy.
#
#   ./start.sh "your goal"
#
# First run: creates the venv, installs deps, checks .env, and launches the
# jiffy Chrome. Log into any sites you need ONCE in that window, then run again.

set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

if [ ! -d .venv ]; then
  echo "==> creating virtualenv"
  "$PY" -m venv .venv
fi

echo "==> installing dependencies"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -c "import playwright" 2>/dev/null || .venv/bin/pip install -q playwright
.venv/bin/playwright install chromium >/dev/null 2>&1 || true

if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "Created .env. Add these two, then run ./start.sh again:"
  echo "  TYPESAFE_API_KEY=..."
  echo "  TEXT_MODEL_API_KEY=..."
  exit 1
fi

missing=0
grep -qE '^TYPESAFE_API_KEY=.+' .env || { echo "Missing TYPESAFE_API_KEY in .env"; missing=1; }
grep -qE '^TEXT_MODEL_API_KEY=.+' .env || { echo "Missing TEXT_MODEL_API_KEY in .env"; missing=1; }
[ "$missing" -eq 0 ] || exit 1

if ! grep -qE '^JIFFY_CDP_URL=.+' .env; then
  echo "==> first-time browser setup"
  ./bin/jiffy setup
  echo
  echo "Log into any sites you need in the jiffy Chrome window (once)."
  echo "Then run:  ./bin/jiffy \"your goal\""
  exit 0
fi

if [ "$#" -eq 0 ]; then
  echo "Usage: ./start.sh \"your goal\""
  echo 'Example: ./start.sh "Open Gmail and summarize the first email"'
  exit 0
fi

exec ./bin/jiffy "$@"
