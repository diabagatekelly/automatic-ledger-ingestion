# Observability — structured webhook telemetry (Issues #34, #44)

Every observable event emits **one structured JSON log line** to stdout via the
shared emitter in `src/telemetry.py`. Cloud Run's logging agent parses that line
into `jsonPayload`, so the fields are directly queryable in Logs Explorer and
countable by a Cloud Monitoring log-based metric — **no external service, no DB,
free-tier only.**

Two event families, one per failure domain:

| `event` | emitted by | counted by |
|---------|-----------|------------|
| `gemini_parse` | `src/llm._generate_note` (text **and** image) | `gemini_parse_counter` |
| `media_download` | `src/main._row_for_image` via `src/media.log_download_failure` | `media_download_counter` |

They are deliberately **separate events**: a failed download never reaches Gemini,
so folding it into `gemini_parse` would pollute those buckets — and keeping it out
without its own event is exactly how a media outage stayed invisible (see below).

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
| `reason`     | on `needs_review`: `no_amount` · `low_confidence` — on `fallback`: `transient_429` · `transient_503` · `bad_request_400` · `no_api_key` · `invalid_api_key` · `empty_response` · `bad_json` · `other` |
| `confidence` | on `success` and `needs_review`: `high` \| `low` — absent on `fallback` |

`no_api_key` vs `invalid_api_key` (#44): the first means the key isn't **mounted**
(fix the secret/deploy); the second means it's mounted but **rejected** — 401/403:
revoked, mistyped, wrong project (fix the key). Same blind-spot family, different
runbooks, and `other` used to hide the latter.

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

## The log-based metric — `gemini_parse_counter`

**There is exactly one, and it answers everything.** One counter over
`jsonPayload.event="gemini_parse"`, sliced by three label extractors:

| label | field | values |
|-------|-------|--------|
| `outcome` | `EXTRACT(jsonPayload.outcome)` | `success` · `needs_review` · `fallback` |
| `reason` | `EXTRACT(jsonPayload.reason)` | the review trigger or the error bucket |
| `confidence` | `EXTRACT(jsonPayload.confidence)` | `high` · `low` |

Every question is a slice of it — no second metric, no ratio between two counters that
have to be kept in sync:

| question | slice |
|----------|-------|
| total parses | sum across labels |
| **fallback rate** — drives #33's durable-queue go/no-go | `{outcome="fallback"}` ÷ total, split by `reason` |
| **needs-review rate** — the owner's manual-fix burden | `{outcome="needs_review"}` ÷ total |
| **real junk** | `{reason="no_amount"}` |
| **candidate noise** — flagged *only* on model self-doubt | `{reason="low_confidence"}` |
| model self-doubt overall | `{confidence="low"}` |

Labels also mean the metric absorbs schema growth: it picked up the third `outcome`
value (`needs_review`) the moment #9 shipped, without being touched.

### Recreating it

Built in the Console (**Logging → Log-based Metrics → Create metric → Counter**), which
handles label descriptors that the `gcloud` flags make fiddly. To do it from the CLI,
use a YAML config — `--log-filter` on the command line is a quoting trap (below):

```yaml
# counter-metric.yaml
description: Gemini parse outcomes, labelled by outcome / reason / confidence
filter: |-
  resource.type="cloud_run_revision"
      AND resource.labels.service_name="catering-ledger-webhook"
      AND jsonPayload.event="gemini_parse"
labelExtractors:
  outcome: EXTRACT(jsonPayload.outcome)
  reason: EXTRACT(jsonPayload.reason)
  confidence: EXTRACT(jsonPayload.confidence)
metricDescriptor:
  metricKind: DELTA
  valueType: INT64
  unit: '1'
  labels:
  - key: outcome
  - key: reason
  - key: confidence
```

```bash
gcloud logging metrics create gemini_parse_counter \
  --project=catering-ledger --config-from-file=counter-metric.yaml
# ...or `update` to change an existing one (adding a label is additive; createTime and
# existing history survive).
gcloud logging metrics describe gemini_parse_counter --project=catering-ledger
```

Then chart it in **Metrics Explorer** as `logging/user/gemini_parse_counter`, grouped by
whichever label the question needs.

> **⚠️ Two `gcloud` traps, both learned the hard way.**
>
> **PowerShell 5.1 mangles the double quotes inside `--log-filter`** on the way to
> `gcloud.cmd`, and a failed create is *quiet*. That's why `gemini_parse_fallbacks` sat
> silently uncreated for a day after #34 shipped while its sibling existed, and every
> chart read a reassuring 0%. Use Git Bash, or the YAML config above.
>
> **Always `describe` after create/update.** A metric that doesn't exist and a metric
> counting zero look identical on a chart.

> **⚠️ Metrics and labels do NOT backfill.** Both only apply to log lines written after
> they exist. A metric created *after* the revision that emits its field silently loses
> the window in between; a metric created *before* just counts zero, which is harmless.
> **So always create the metric first, then deploy the code that emits the field.**
>
> `gemini_parse_counter` counts from **2026-07-15 13:55 UTC**; the `reason` label was
> added **16:37 UTC**, before #9's outcome/reason values ever reached prod — so no
> window is missing for them. Trust it from 2026-07-15 forward.

### Why only one metric

There were briefly five: `gemini_parse_total`, `gemini_parse_fallbacks`,
`gemini_low_confidence`, `gemini_needs_review` and this one. Every one of the first four
turned out to be a slice of this metric's labels, so they were deleted on 2026-07-15.
**Reach for a label before a new metric** — a single-purpose counter answers one
question forever, while a label answers questions you haven't thought of yet (`reason`
is the proof: it only became the interesting field *after* the metrics were built).

## The `media_download` event — why a second family exists (Issue #44)

If `download_media` fails, `_row_for_image` falls back to an unreadable-image
marker row **without ever reaching Gemini** — so no `gemini_parse` line fires and
`gemini_parse_counter` doesn't move. Before #44 that made a WhatsApp media / token
outage **invisible to every dashboard**: the #5 expired-token incident (2026-07-13)
filled the Sheet with `[unreadable image]` rows while every chart stayed green, and
it was caught by the owner noticing bad rows, not by us.

### The log schema

```json
{"severity":"WARNING","event":"media_download","outcome":"failure","reason":"auth_401"}
```

| field | values |
|-------|--------|
| `event` | always `media_download` |
| `outcome` | `failure` (successful downloads are not logged — the parse line that follows implies them) |
| `reason` | `auth_401` · `not_found` · `timeout` · `other` |

Always `WARNING`: unlike a `needs_review` row, a failed download means the
receipt's content is genuinely lost to the ledger.

- **`auth_401`** — the Graph API rejected the access token. **This is the
  token-expiry signature** (the #5 incident), split out deliberately so it's
  distinguishable from a transient network failure without reading tracebacks.
  A sustained run of `auth_401` = re-provision the token, then **redeploy**
  (`:latest` resolves at deploy time).
- **`not_found`** — the media id didn't resolve. Graph reports an unknown or
  malformed id as a **400** GraphMethodException (verified live, 2026-07-16),
  so 400 and 404 both land here; 404 covers the short-lived download URL going
  stale between the two hops.
- **`timeout`** — either hop exceeded the request timeout.
- **`other`** — everything else (5xx, metadata missing its download URL).

Classification lives in `src/media.classify_download_error`; the smoke script
`scripts/smoke-media.py` proves the two live-triggerable buckets (`auth_401`,
`not_found`) against the **real** media endpoint — unlike
`scripts/smoke-gemini-image.py`, which reads bytes from disk and can't see a
dead token.

### Read it in Logs Explorer

```
resource.type="cloud_run_revision"
resource.labels.service_name="catering-ledger-webhook"
jsonPayload.event="media_download"
```

Token-expiry watch — the #5 signature specifically:

```
jsonPayload.event="media_download" AND jsonPayload.reason="auth_401"
```

### The log-based metric — `media_download_counter`

Same shape as `gemini_parse_counter`: one counter, `reason`/`outcome` labels,
every question a slice. (A second *metric* rather than more labels on the first
because it's a different **event family** with its own filter — the
label-before-metric rule applies to questions within a family, not across them.)

```yaml
# media-download-counter.yaml
description: WhatsApp media-download failures, labelled by outcome / reason
filter: |-
  resource.type="cloud_run_revision"
      AND resource.labels.service_name="catering-ledger-webhook"
      AND jsonPayload.event="media_download"
labelExtractors:
  outcome: EXTRACT(jsonPayload.outcome)
  reason: EXTRACT(jsonPayload.reason)
metricDescriptor:
  metricKind: DELTA
  valueType: INT64
  unit: '1'
  labels:
  - key: outcome
  - key: reason
```

```bash
gcloud logging metrics create media_download_counter \
  --project=catering-ledger --config-from-file=media-download-counter.yaml
gcloud logging metrics describe media_download_counter --project=catering-ledger
```

Chart as `logging/user/media_download_counter` grouped by `reason`. The gcloud
traps above (PowerShell quoting, describe-after-create, **no backfill** — create
the metric before deploying the emitting code) all apply.

> `media_download_counter` counts from **2026-07-16**, created before the #44
> code deployed — so no window is missing.

## Why this decides the durable-retry-queue go/no-go (#33 follow-up)

`gemini_parse_counter{outcome="fallback"}` grouped by `reason` is the data the queue
decision needs: a high `transient_429`/`transient_503` share *after* #33's in-request
retry means a durable queue would pay off; a low one means it wouldn't. This slice
produces the numbers so that call is data-driven, not a guess.

The same metric answers #9's open question from the other direction:
`{reason="low_confidence"}` versus `{reason="no_amount"}` says how much of the owner's
`NEEDS_REVIEW` burden is real junk and how much is the model second-guessing itself on
rows that were fine. If it's mostly the latter and she says the flag isn't earning its
keep, drop that trigger — one line in `llm.review_reason`.
