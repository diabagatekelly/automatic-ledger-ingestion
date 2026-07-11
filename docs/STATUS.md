# STATUS — live handoff log

> Update this at the end of every working session. Newest state at the top.
> Keep it short and current: **where we are, what's next, and why.**

## Snapshot
- **Last updated:** 2026-07-11
- **Phase:** M1 (Walking Skeleton) — foundation complete, no slices implemented yet.
- **Next action:** Implement **Issue #1** (text payload → one row in the Sheet). It deliberately
  de-risks Google Sheets auth + deploy before any AI/messaging.
- **Health:** CI green · coverage 100% · `main` protected · secret scanning + push protection on.

## Done
- Repo scaffolded and public: https://github.com/diabagatekelly/catering-ledger
- Webhook skeleton with WhatsApp verification handshake (`src/main.py`) + tests.
- Quality gate wired (ruff/black/mypy strict/pytest) and passing in CI.
- CI/CD workflows: `ci.yml` active; `deploy.yml` keyless (WIF), manual until Issue #3.
- Milestones M1–M4, 10 vertical-slice issues, and [project board #5](https://github.com/users/diabagatekelly/projects/5).

## Next up (by milestone)
- **M1:** #1 text→row · #2 WhatsApp→webhook · #3 CI/CD (GCP project + WIF, flip deploy to auto)
- **M2:** #4 parse text note · #5 parse receipt photo
- **M3:** #6 SUMIFS totals · #7 month override + contract filter
- **M4:** #8 confirmation reply · #9 low-confidence handling · #10 auditor report

## Decisions & why (don't re-litigate without new info)
- **Compute = Cloud Functions, not Apps Script.** Apps Script was cheaper/simpler but teaches
  ~no transferable cloud skills; this is a real tool that should also grow cloud skills.
- **LLM = Gemini 2.5 Flash.** Chosen for the genuinely free multimodal tier (~250 req/day),
  which covers image *and* audio — so no separate speech-to-text service is needed.
- **Ingestion = WhatsApp Cloud API.** The owner already uses it; inbound + service replies are
  free (unlimited since Nov 2024, within the 24h service window). Telegram was the runner-up.
- **Storage = Google Sheets.** It's the DB, the owner's UI, and the auditor's dashboard at once.
- **CD = keyless via Workload Identity Federation.** No service-account JSON stored anywhere.
- **Repo = public** with secret scanning + push protection as the safety net (serves showcase goal).

## Gotchas
- Shell noise: harmless `ng help` / "analytics" error from an Angular CLI hook in the user profile.
- Local Python 3.11 vs CI 3.12 — run `black --target-version py311` locally.

## Handoff checklist (do before stopping)
- [ ] Update the **Snapshot** (last updated, phase, next action).
- [ ] Move finished items from *Next up* to *Done*; note any new decisions/gotchas.
- [ ] Ensure CI is green and changes are committed/merged.
