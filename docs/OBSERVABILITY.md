# Observability — Gemini parse-outcome telemetry (Issue #34)

Every Gemini parse (text **and** image, via the shared `src/llm._generate_note`)
emits **one structured JSON log line** to stdout. Cloud Run's logging agent parses
that line into `jsonPayload`, so the fields are directly queryable in Logs Explorer
and countable by a Cloud Monitoring log-based metric — **no external service, no DB,
free-tier only.**

## The log schema

```json
{"severity":"INFO",   "event":"gemini_parse","outcome":"success","confidence":"high"}
{"severity":"INFO",   "event":"gemini_parse","outcome":"success","confidence":"low"}
{"severity":"WARNING","event":"gemini_parse","outcome":"fallback","reason":"transient_503"}
```

| field        | values |
|--------------|--------|
| `event`      | always `gemini_parse` (the stable filter key) |
| `outcome`    | `success` \| `fallback` |
| `confidence` | present only on `success`: `high` \| `low` |
| `reason`     | present only on `fallback`: `transient_429` · `transient_503` · `bad_request_400` · `no_api_key` · `empty_response` · `bad_json` · `other` |

The `reason` buckets come from `src/llm._classify_error`, which is the **single
source of truth** shared with #33's retry decision (transient = `transient_429`/
`transient_503`) — so "what we retry" and "what we count as transient" can't drift.

A `fallback` means the message still landed as a **raw-text row** (nothing is lost);
the telemetry just tells us how often, and why, the structured parse degraded.

### ⚠️ `outcome` alone does not mean the answer was good — read `confidence` too

`outcome` measures **mechanical** failure only: whether Gemini *answered*, not
whether the answer was usable.

A blurry receipt, a photo of a mat, a screenshot, an off-topic text — none of these
log a `fallback`. Gemini is asked for JSON and dutifully returns well-formed JSON
with empty fields and `confidence: "low"`, which is a **`success`** by this metric
and a junk row in the Sheet.

**`confidence` is the field that exposes that** (#9). A `success` with
`confidence="low"` means "Gemini answered, but couldn't actually find a ledger entry" —
count those, not just fallbacks. `confidence` is the *coerced* value (the one that
lands in the row), so the logs and the Sheet can't disagree.

So don't test the telemetry by sending a bad photo — you'll get a `success` line, just
a low-confidence one. Fallbacks are **rare by design** (a real 429/503 after retries,
a dead API key, a blocked response); normal messages log `success`. That rarity is the
point — it's what the metric quantifies.

Still open in #9: flagging those rows `NEEDS_REVIEW` for the owner. The telemetry half
only makes the problem *countable*.

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

**Junk rows the owner will have to fix by hand** — parses that "succeeded" without
finding a real ledger entry. This is the query for "is the aunt getting garbage rows":

```
jsonPayload.event="gemini_parse" AND jsonPayload.confidence="low"
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

Three counter metrics, giving two independent rates over `gemini_parse_total`:

| rate | numerator | what it tells you |
|------|-----------|-------------------|
| **fallback rate** | `gemini_parse_fallbacks` | how often Gemini *failed* → drives #33's durable-queue go/no-go |
| **low-confidence rate** | `gemini_low_confidence` | how often Gemini *succeeded but found nothing usable* → drives #9's `NEEDS_REVIEW` work |

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

# Low-confidence successes — the junk rows (#9)
gcloud logging metrics create gemini_low_confidence \
  --project=catering-ledger \
  --description="Parses that succeeded but found no usable ledger entry" \
  --log-filter='resource.type="cloud_run_revision"
    AND resource.labels.service_name="catering-ledger-webhook"
    AND jsonPayload.event="gemini_parse" AND jsonPayload.confidence="low"'
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
> | `gemini_low_confidence` | create it **before** the #9 deploy — see below |
>
> So: trust the **fallback rate** from 2026-07-15 forward only.
>
> **Create `gemini_low_confidence` before the revision that emits `confidence` goes
> live.** A metric that predates its field simply counts zero — harmless — whereas a
> metric created afterwards silently loses the window in between. Create early; it
> costs nothing and closes the gap that bit `gemini_parse_fallbacks`.

### Optional: a single reason-labelled metric

For a dashboard split by `reason` in one metric, add it in the Console
(**Logging → Log-based Metrics → Create metric → Counter**), filter
`jsonPayload.event="gemini_parse"`, and add a **label** `reason` with field name
`jsonPayload.reason`. The Console handles the label descriptor the `gcloud` flags
make fiddly. The two counters above already satisfy the acceptance (rate over time);
this is just nicer for dashboards.

## Why this decides the durable-retry-queue go/no-go (#33 follow-up)

`gemini_parse_fallbacks` split by `reason` is the data the queue decision needs:
a high `transient_429`/`transient_503` share *after* #33's in-request retry means a
durable queue would pay off; a low one means it wouldn't. This slice produces the
numbers so that call is data-driven, not a guess.
