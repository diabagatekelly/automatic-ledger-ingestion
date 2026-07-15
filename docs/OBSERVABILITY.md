# Observability — Gemini parse-outcome telemetry (Issue #34)

Every Gemini parse (text **and** image, via the shared `src/llm._generate_note`)
emits **one structured JSON log line** to stdout. Cloud Run's logging agent parses
that line into `jsonPayload`, so the fields are directly queryable in Logs Explorer
and countable by a Cloud Monitoring log-based metric — **no external service, no DB,
free-tier only.**

## The log schema

```json
{"severity":"INFO",   "event":"gemini_parse","outcome":"success","confidence":"high"}
{"severity":"INFO",   "event":"gemini_parse","outcome":"needs_review","reason":"low_confidence","confidence":"low"}
{"severity":"INFO",   "event":"gemini_parse","outcome":"needs_review","reason":"no_amount","confidence":"low"}
{"severity":"WARNING","event":"gemini_parse","outcome":"fallback","reason":"transient_503"}
```

| field        | values |
|--------------|--------|
| `event`      | always `gemini_parse` (the stable filter key) |
| `outcome`    | `success` · `needs_review` · `fallback` |
| `reason`     | on `needs_review`: `no_amount` · `low_confidence` — on `fallback`: `transient_429` · `transient_503` · `bad_request_400` · `no_api_key` · `empty_response` · `bad_json` · `other` |
| `confidence` | on `success` and `needs_review`: `high` \| `low` — absent on `fallback` |

### The three outcomes

| outcome | what happened | the row |
|---------|---------------|---------|
| **`success`** | parsed cleanly | lands, unflagged |
| **`needs_review`** | parsed, but missing info or bogus data | lands **flagged `NEEDS_REVIEW`** in Source/Notes |
| **`fallback`** | the parse itself failed | lands as **raw text** — nothing is lost |

Only `fallback` is a `WARNING`. A `needs_review` row **landed**; nothing broke, and
this outcome is expected to fire regularly, so raising it above INFO would just train
the reader to ignore the level.

`outcome` is decided by `src/llm.review_reason` — the **same call**
`sheets.build_row_from_note` makes to place the flag. One rule, so a row logged
`needs_review` is exactly a row flagged in the Sheet; they cannot drift.
The `fallback` buckets come from `src/llm._classify_error`, likewise shared with #33's
retry decision (transient = `transient_429`/`transient_503`).

### Why `needs_review` exists (and why `success` used to lie)

`outcome` used to be two-state, and it measured only whether Gemini *answered* — never
whether the answer was any good. A blurry receipt, a photo of a mat, an off-topic text:
Gemini is asked for JSON and dutifully returns well-formed JSON with empty fields, so
all of them logged plain **`success`** while junk landed in the Sheet. The dashboards
stayed green precisely when the ledger was degrading, and "the ledger looks clean" was
unfalsifiable.

### Reading `reason` on a `needs_review` — this is the useful part

The two buckets are **not** equally serious, and the split is the point:

- **`no_amount`** — a *fact* about the row: blank, non-numeric, or **zero** amount.
  Genuinely unusable. This is the mat / blurry receipt / contentless-text case, and
  the only one that earns a reply asking the owner to clarify.
- **`low_confidence`** — only the model's *opinion of itself*, and a poorly calibrated
  one. The row has a real amount and is often perfectly fine:

> Live smoke run, 2026-07-15 (real Gemini): `"Cash sale, $200, Wedding Cake"` → a
> complete, correct row (Amount 200, Revenue, Event "Wedding Cake", Paid) scored
> **`low`**, while a *sparser* row scored **`high`**. The better row scored worse.

So **`reason="low_confidence"` is the candidate-noise bucket**: rows flagged *only*
because the model doubted itself. If that share is large and the owner reports the flag
isn't earning its keep, drop the trigger — it's one line in `llm.review_reason`, and
the two triggers are kept separate for exactly that.

A row can trip **both** (the live `"Recu paiement"` returned `amount: 0` *and*
`confidence: low`). `no_amount` wins — a metric label needs one value, and the fact
beats the opinion.

> **Don't test this by sending a bad photo expecting a `fallback`.** You'll get a
> `needs_review`. Fallbacks are **rare by design** (a real 429/503 surviving retries, a
> dead API key, a blocked response) — that rarity is what the metric quantifies.

## Read it in Logs Explorer

All parse outcomes (last hour, newest first):

```
resource.type="cloud_run_revision"
resource.labels.service_name="catering-ledger-webhook"
jsonPayload.event="gemini_parse"
```

Only fallbacks, broken down by reason — add `jsonPayload.outcome="fallback"` and
group by `jsonPayload.reason` (the "Analyze" / field breakdown panel), or:

```
jsonPayload.event="gemini_parse" AND jsonPayload.outcome="fallback"
```

**Flagged rows** — everything that landed with `NEEDS_REVIEW` in the Sheet. Group by
`jsonPayload.reason` in the "Analyze" panel to get the `no_amount` vs `low_confidence`
split, which is the number worth watching:

```
jsonPayload.event="gemini_parse" AND jsonPayload.outcome="needs_review"
```

**Genuinely broken rows only** — no usable amount. This is "is the aunt getting
garbage?", and it excludes the rows flagged merely because the model doubted itself:

```
jsonPayload.event="gemini_parse" AND jsonPayload.reason="no_amount"
```

**Candidate noise** — rows flagged *only* on low confidence. If this dwarfs
`no_amount` and the owner says the flag isn't useful, that trigger is the thing to drop:

```
jsonPayload.event="gemini_parse" AND jsonPayload.reason="low_confidence"
```

`gcloud` equivalent (swap the last clause for `confidence="low"` to chase junk rows):

```bash
gcloud logging read \
  'resource.type="cloud_run_revision"
   AND resource.labels.service_name="catering-ledger-webhook"
   AND jsonPayload.event="gemini_parse" AND jsonPayload.outcome="fallback"' \
  --project=catering-ledger --freshness=24h \
  --format='table(timestamp, jsonPayload.reason)'
```

## Log-based metrics

Counter metrics, each giving a rate over `gemini_parse_total`:

| rate | numerator | what it tells you |
|------|-----------|-------------------|
| **fallback rate** | `gemini_parse_fallbacks` | how often the parse *failed* → drives #33's durable-queue go/no-go |
| **needs-review rate** | `gemini_needs_review` | how often a row lands **flagged** — the owner's manual-fix burden |
| **low-confidence rate** | `gemini_low_confidence` | how often the model doubts itself, regardless of whether the row is usable |

`gemini_needs_review` is the one to watch, **split by `reason`** — `no_amount` is real
junk, `low_confidence` is candidate noise. `gemini_low_confidence` predates the
three-state outcome and largely overlaps it; it's kept because it's already collecting
and measures a subtly different thing (self-doubt, not usability).

They're plain, verified `gcloud` counters (no label-extractor gymnastics); the
per-`reason` split is read from `jsonPayload.reason` in Logs Explorer (above) or by
adding a label in the metric's console UI.

```bash
# Total parses
gcloud logging metrics create gemini_parse_total \
  --project=catering-ledger \
  --description="Gemini parse attempts (success + fallback)" \
  --log-filter='resource.type="cloud_run_revision"
    AND resource.labels.service_name="catering-ledger-webhook"
    AND jsonPayload.event="gemini_parse"'

# Fallbacks only
gcloud logging metrics create gemini_parse_fallbacks \
  --project=catering-ledger \
  --description="Gemini parses that fell back to a raw-text row" \
  --log-filter='resource.type="cloud_run_revision"
    AND resource.labels.service_name="catering-ledger-webhook"
    AND jsonPayload.event="gemini_parse" AND jsonPayload.outcome="fallback"'

# Low-confidence successes — screening signal for junk rows (#9)
gcloud logging metrics create gemini_low_confidence \
  --project=catering-ledger \
  --description="Parses where Gemini reported low confidence (screening signal, not a junk count)" \
  --log-filter='resource.type="cloud_run_revision"
    AND resource.labels.service_name="catering-ledger-webhook"
    AND jsonPayload.event="gemini_parse" AND jsonPayload.confidence="low"'

# Rows that landed FLAGGED — split by reason to separate junk from noise (#9)
gcloud logging metrics create gemini_needs_review \
  --project=catering-ledger \
  --description="Rows that landed flagged NEEDS_REVIEW (see reason: no_amount vs low_confidence)" \
  --log-filter='resource.type="cloud_run_revision"
    AND resource.labels.service_name="catering-ledger-webhook"
    AND jsonPayload.event="gemini_parse" AND jsonPayload.outcome="needs_review"'
```

> **⚠️ Running these on Windows.** The commands above are written for bash. In
> PowerShell 5.1 the double quotes inside `--log-filter` get mangled on the way to
> `gcloud.cmd` and the create can fail — this is why `gemini_parse_fallbacks` was
> silently missing for a day after #34 shipped while `gemini_parse_total` existed.
> Either run them in Git Bash, or sidestep the quoting with a YAML config file:
>
> ```powershell
> # fallbacks-metric.yaml:  description: <text>  /  filter: |- <the filter>
> & $gcloud logging metrics create gemini_parse_fallbacks `
>     --project=catering-ledger --config-from-file=fallbacks-metric.yaml
> ```
>
> Always verify after creating: `gcloud logging metrics describe <name> --project=catering-ledger`.

Then in **Metrics Explorer** chart either numerator ÷ `logging/user/gemini_parse_total`
(a ratio) for that rate over any window. **Chart both** — they fail in opposite
directions, and the low-confidence rate is the one that catches a quietly-degrading
ledger while the fallback rate sits at a reassuring zero.

> **⚠️ A ratio lies for any window before BOTH its metrics existed.** Log-based
> metrics **do not backfill** — they only count lines written after the metric is
> created. A window where the numerator metric didn't yet exist charts a
> **falsely perfect 0%** over a real denominator: not "healthy", just "not counting".
>
> | metric | counting since |
> |--------|----------------|
> | `gemini_parse_total` | 2026-07-14 23:26 UTC |
> | `gemini_parse_fallbacks` | 2026-07-15 12:50 UTC |
> | `gemini_low_confidence` | 2026-07-15 13:13 UTC |
> | `gemini_parse_counter` (labelled) | 2026-07-15 13:55 UTC |
> | `gemini_needs_review` | 2026-07-15 16:28 UTC — **before** the #9 deploy, deliberately |
>
> So: trust the **fallback rate** from 2026-07-15 forward only.
>
> **Always create the metric before the revision that emits its field.** A metric that
> predates its field counts zero — harmless — whereas one created afterwards silently
> loses the window in between. That's what left `gemini_parse_fallbacks` blind for a
> day. Creating early costs nothing.

### `gemini_parse_counter` — the labelled metric (exists; prefer it for dashboards)

Created in the Console 2026-07-15, this is **one** metric over
`jsonPayload.event="gemini_parse"` with label extractors:

| label | field |
|-------|-------|
| `outcome` | `EXTRACT(jsonPayload.outcome)` |
| `confidence` | `EXTRACT(jsonPayload.confidence)` |

It subsumes most of the separate counters above — slice by `outcome` for the fallback
and needs-review rates, by `confidence` for self-doubt, and sum across labels for the
total. It also picked up the third `outcome` value (`needs_review`) for free when #9
landed, without touching the metric.

**⚠️ It does not yet label `reason`** — which is now the field that matters, since it
separates real junk (`no_amount`) from candidate noise (`low_confidence`), and the
error buckets on `fallback`. Add a third label `reason` → `EXTRACT(jsonPayload.reason)`
in the Console (**Logging → Log-based Metrics →** edit → **Add label**); the Console
handles the label descriptor that the `gcloud` flags make fiddly. Note a label added
now only applies to logs written from that point on — same no-backfill rule as the
metrics themselves.

The standalone counters are kept because they're already collecting and cost nothing,
but for a dashboard, prefer this one.

## Why this decides the durable-retry-queue go/no-go (#33 follow-up)

`gemini_parse_fallbacks` split by `reason` is the data the queue decision needs:
a high `transient_429`/`transient_503` share *after* #33's in-request retry means a
durable queue would pay off; a low one means it wouldn't. This slice produces the
numbers so that call is data-driven, not a guess.
