# CLAUDE.md — orientation for any Claude instance

> **Start every session here:**
> 1. Read this file.
> 2. Read [`docs/STATUS.md`](docs/STATUS.md) — the live handoff (where we are, what's next, why).
> 3. Check open work: `gh issue list` and the [project board](https://github.com/users/diabagatekelly/projects/5).
>
> **End every session:** update `docs/STATUS.md` (state, next action, decisions, gotchas) and commit it.

## What this is
Automated near-zero-cost catering accounting. The owner sends a WhatsApp photo of a
receipt or a text/voice note; a serverless webhook parses it with a multimodal LLM and
appends a row to a Google Sheet that is also the owner's UI and the auditor's dashboard.
Design principle: **fire-and-forget** for the owner, **near-zero cost/maintenance** for the dev.

## Stack
GCP Cloud Functions (Gen2, Python 3.12) · Gemini 2.5 Flash (multimodal) ·
WhatsApp Cloud API · Google Sheets · GitHub Actions (CI + keyless CD via WIF).

## Repo map
- `src/main.py` — Cloud Function entry (`webhook`); pure helpers kept testable.
- `tests/` — pytest; keep coverage ≥ 70% (currently 100%).
- `docs/ARCHITECTURE.md` — **the LLM JSON schema + Sheet layout every slice reuses.**
- `docs/STATUS.md` — live progress/handoff log.
- `.github/workflows/` — `ci.yml` (gate), `deploy.yml` (keyless deploy, manual until Issue #3).

## Working conventions
- **Thin vertical slices.** Each issue is demoable end-to-end. Don't build horizontal layers.
- **`main` is protected.** No direct pushes; open a PR, the `quality` check must pass to merge.
- **Secrets never in git.** Use env / GCP Secret Manager. `.gitignore` blocks `.env`, keys, SA JSON.
- **Reuse the data contract** in `docs/ARCHITECTURE.md` across text, image, and voice slices.

## Local dev
```bash
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt -r requirements-dev.txt
ruff check . && black --check . && mypy src && pytest   # same gate as CI
functions-framework --target=webhook --debug            # run locally
```

## Known gotchas
- The shell prints a harmless `ng help` / "analytics" error (an Angular CLI completion hook in
  the user's profile). Ignore it.
- Local Python is 3.11; CI is 3.12. Code targets both. Run `black` with `--target-version py311`
  locally to silence the AST safety warning.
