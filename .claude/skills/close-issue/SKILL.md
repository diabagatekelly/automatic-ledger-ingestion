---
name: close-issue
description: Run the standard close-out checklist for a completed vertical slice whose PR is open (or about to be). Covers the local quality gate, live/manual validation, the plugin review pass (code-review-protocol, unit-test-reviewer, quality-assurance, security-compliance) plus Sourcery follow-up, a manual-testing guide for the owner, and the STATUS.md handoff. Use when finishing an issue, opening a PR, or updating a PR after review.
---

# Close out a vertical slice

The repeatable "definition of done" for this project. Run it whenever a slice's
code is finished and its PR is open. **Scale depth to diff size** — a ~40-line
slice does not need every review agent; a large or risky one does. Don't
gold-plate (see `docs/WORKFLOW.md` guardrail).

## 1. Local quality gate — must be green
Same gate as CI (`.github/workflows/ci.yml`). Run from the repo root:
```bash
./.venv/Scripts/python.exe -m ruff check .
./.venv/Scripts/python.exe -m black --check --target-version py311 .
./.venv/Scripts/python.exe -m mypy src
./.venv/Scripts/python.exe -m pytest --cov=src --cov-report=term-missing --cov-fail-under=70
```
Auto-fix format/imports with `ruff check --fix .` and `black --target-version py311 .`.

## 2. Live / manual validation
Green tests are not enough — confirm the real-world effect. Start the function
and exercise it against real dependencies (see the "Local run / manual test"
section of `docs/STATUS.md` for the exact command + env vars), e.g. curl/Postman
a payload and verify the row actually lands in the Sheet.

## 3. Open / update the PR
PR body structure: **What · How · Tests + coverage · Live acceptance · Notes/follow-ups**.
End with `Closes #N`. Keep secrets out — confirm `.env` / `service-account.json`
are not staged (`git status --ignored`).

## 4. Post-PR review pass (the plugins)
Run these against the diff and give the owner a consolidated, severity-ranked summary:
- **`flexion-engineer-base:code-review-protocol`** (skill) — structured review:
  security, likely bugs, performance, complexity/YAGNI, missing tests, edge cases.
- **`flexion-engineer-base:unit-test-reviewer`** (agent) — test quality: no mocking
  the code under test, behavior-not-implementation, useless/duplicate tests, coverage
  gaps. For a small diff, apply its criteria inline; for a large diff, spawn the agent.
- **`flexion-ai-quality-assurance:quality-assurance`** (skill) — coverage + is the
  slice genuinely demo-able end-to-end.
- **`flexion-ai-security-compliance:security-compliance`** (skill) — secrets, PII,
  injection, credential handling.
- **Sourcery** — runs automatically on the PR. Read its comments; address the real
  ones via TDD (red → green), and note which you defer and why (YAGNI).
Fix the "do it now" findings on the branch; defer the rest with a one-line reason.

## 5. Manual-testing guide for the owner
Hand over Postman/curl recipes for **each path the slice exposes** — happy path plus
any notable edge/security cases (e.g. the formula-injection guard, verification GET,
405 on other methods). Include method, URL, body type, and expected status/effect.

## 6. Update STATUS.md & hand off
- Move the issue from *Next up* → *Done* (one-line summary: what shipped, test/coverage).
- Set the new **Active task / Next action**; record any decisions & gotchas.
- Wait for the **owner** to merge (main is protected — never auto-merge).
- After merge: `git checkout main && git pull --ff-only`, then delete the feature branch.

## Deferred to a later issue
`flexion-ai-devops-tools` + the `gha-security` skill belong to **Issue #3** (GCP + keyless
WIF deploy), not the per-slice close-out.
