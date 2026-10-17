#!/bin/sh
# Start ResuApply on macOS or Linux: ./start.sh
# The first start creates .venv (Python 3.12, or uv), installs the packages and creates .env from
# .env.example; later starts update the packages only when requirements.txt changed. Then the app
# runs at http://127.0.0.1:8000 and opens in your browser. Stop it with Ctrl+C.
set -eu
cd "$(dirname "$0")"

is_312() { "$1" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' >/dev/null 2>&1; }

if [ ! -x .venv/bin/python ] || ! is_312 .venv/bin/python; then
  rm -rf .venv
  made=""
  for candidate in python3.12 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && is_312 "$candidate"; then
      "$candidate" -m venv .venv && made=1
      break
    fi
  done
  if [ -z "$made" ] && command -v uv >/dev/null 2>&1; then
    uv venv -q --python 3.12 .venv && made=1
  fi
  if [ -z "$made" ]; then
    echo "ResuApply needs Python 3.12. Install it from https://www.python.org/downloads/"
    echo "(or install uv from https://docs.astral.sh/uv/), then run ./start.sh again."
    exit 1
  fi
fi

.venv/bin/python -m scripts.ensure_packages

if [ ! -e .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env from .env.example. Add your OPENROUTER_API_KEY there to use the AI features."
fi

exec .venv/bin/python -m app --open
