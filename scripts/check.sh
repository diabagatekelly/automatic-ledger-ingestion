#!/usr/bin/env bash
# Quality gate — the single source of truth shared by CI
# (.github/workflows/ci.yml) and the /close-issue skill, so the two never drift.
#
# Run from the repo root with the project tools on PATH: activate the venv
# locally (`source .venv/Scripts/activate`); CI installs them globally.
set -euo pipefail

ruff check .
black --check --target-version py311 .
mypy src
pytest --cov=src --cov-report=term-missing --cov-fail-under=70
