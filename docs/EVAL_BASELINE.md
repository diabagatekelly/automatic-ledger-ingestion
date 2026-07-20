# Gemini parse — accuracy eval baseline (Issue #30)

A repeatable, **scored** eval of the model + prompt against a labeled dataset, so
we know *quantitatively* which field is weakest and catch regressions whenever we
change the prompt or model. This is distinct from the unit tests, which validate
**our** coercion/mapping/fallback against a *mocked* Gemini (deterministic, in
CI). This validates the **model + prompt** against real inputs (live, scored, not
in CI — nondeterministic and costs quota).

## How to run

```bash
export GEMINI_API_KEY=...              # from Google AI Studio (see .env.example)
python scripts/eval-gemini.py          # full dataset -> prints the scorecard
python scripts/eval-gemini.py --limit 3   # quick smoke
```

- **Dataset:** `evals/dataset.jsonl` — one labeled case per line (`text` or
  `image` kind), each with the five scored fields + an optional
  `expected_confidence` for deliberately-ambiguous cases.
- **Receipt photos are LOCAL-only.** The `image` cases point at real customer
  receipts (business/client PII) that are **gitignored** (`evals/images/`) so the
  public repo never publishes them. The runner **skips** any image case whose
  file is absent, so the text half stays reproducible for anyone cloning; the
  baseline below (with the image cases) was produced on the machine that has the
  photos on disk. To reproduce the image half, drop the receipts in
  `evals/images/` under the filenames the dataset references.
- **Scorer:** `evals/scoring.py` (pure, unit-tested in `tests/test_eval_scoring.py`).
- **Reproducibility:** every case parses against a **fixed reference date**
  (`--reference-date`, default `2026-07-20`), never `date.today()` — otherwise the
  "no date stated → use today" and relative-date ("yesterday") cases would drift
  and their expected labels would rot. Change the ref date and you must re-resolve
  the dataset's expected dates.
- **Pacing:** `--sleep` (default 4s between calls) keeps a full run under the free
  tier's ~15 requests/min. Without it a `429` mid-run falls back and pollutes the
  score (the webhook's own bounded retry, #33, caps at ~1.5s — far short of the
  ~21s a free-tier 429 asks for). Set `--sleep 0` on a paid key.

## Scoring rules (why the numbers mean what they mean)

- **Scored fields:** `date`, `contract_name`, `category`, `type`, `amount`.
  `notes` is free prose; `event`/`status` are often absent from short notes.
- **amount** compares numerically ("200" == "200.0"), but a blank is distinct
  from any number — a *missing* amount is a different outcome from a zero. Other
  fields are case- & whitespace-insensitive string matches.
- **per-field accuracy** is over PARSED cases only, so an infra fallback can't
  masquerade as a field the model got wrong.
- **overall exact-match** and **fallback rate** are over ALL cases — a fallback is
  a real miss for the owner, just not a *wrong-answer* miss, so it's reported
  separately.
- **confidence** is scored only on the ambiguous cases we labeled `low`.

## Baseline — 2026-07-20

Model alias `gemini-flash-lite-latest`, which currently resolves to
**`gemini-3.1-flash-lite`** (seen in a `429` quota payload — the alias tracks the
live model, so record the resolved id alongside it). 22 cases (18 text + 4 real
receipt photos), `--sleep 4`.

```
overall exact-match: 77%   (17/22)
fallback rate:       0%
per-field accuracy (over parsed cases):
  date           100%
  contract_name   91%
  category         86%
  type             86%
  amount           91%
confidence (ambiguous cases): 100%  (3 labelled)
latency: p50 1.92s  p95 3.82s
```

### What the five misses tell us (all informative, none infra)

- **All three real receipt photos scored *exact*** — including a handwritten
  French/CFA invoice (ONEP, 555 000) and an electronic invoice carrying **two
  dates** (`img-bad-cafe-revenue`: invoice `09/04/2026` vs. an order date of
  `03 avril`), where the model correctly picked the invoice date. So the image
  path is strong on genuine documents; the misses are all edge cases.
- **`contract_name` (2 misses) is the fuzziest field** — both misses are "who
  counts as the contract":
  - *meat-owed-by-us* — the model treated the **supplier** ("the butcher") as the
    contract; our contract is the *client/account who pays us*, so a supplier we
    pay should be blank. A real, actionable model-vs-schema gap.
  - *fall-birthday-iso-date* — "the Fall **family**" vs. the label `Fall`: a
    surname-vs-phrase mismatch, i.e. more a labeling-strictness nuance than a
    clear error. Kept as a miss so the baseline stays honest about how brittle an
    exact contract-name match is.
- **The junk / non-receipt cases (3) are where `category`/`type`/`amount` slip**
  (`Reçu paiement`, `some stuff for the event`, and the bogus cartoon-mat photo).
  The correct behavior is to leave those fields blank; instead the model
  **guesses** a `Revenue`/`Revenue` pair. Notably the **image** junk case
  (`img-bogus-not-a-receipt`) got `amount` *right* (left it **blank**), while the
  two **text** junk notes fabricated an amount — the #9 `amount: 0` invention. So
  the model over-guesses less on an obviously-non-receipt image than on a
  contentless text.
- **Confidence is well-calibrated *down* but over-fires *up*:** all three
  ambiguous cases (incl. the bogus photo) were correctly flagged `low` (100%),
  **but** a complete, correct row (`cash-sale-usd`) *also* scored `low`. So
  `confidence=low` means "the model thinks it guessed", not "this row is junk" —
  which is exactly why #9 only sends a clarifying reply on a **missing amount**,
  not on low confidence alone.

## Reading the result as a regression gate

Re-run after any prompt or model change and compare to the block above. A drop in
a per-field number points at the field to tune; a jump in **fallback rate** points
at infra/quota (or a retired model — the `-latest` alias moving under us), not the
prompt. The two ambiguous cases double as a guard that the model keeps flagging
junk `low` even as the prompt evolves.
