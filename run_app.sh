#!/usr/bin/env bash
# One-command launcher: creates a venv on first run, installs deps, opens the app in your browser.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then python3 -m venv .venv; ./.venv/bin/pip install -q -r requirements.txt; fi
[ -f .env ] && set -a && . ./.env && set +a
PYTHONPATH=src ./.venv/bin/python -m ssm.app "$@"
