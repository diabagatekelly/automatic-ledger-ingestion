# Observability — Gemini parse-outcome telemetry (Issue #34)

Every Gemini parse (text **and** image, via the shared `src/llm._generate_note`)
emits **one structured JSON log line** to stdout. Cloud Run's logging agent parses
that line into `jsonPayload`, so the fields are directly queryable in Logs Explorer
and countable by a Cloud Monitoring log-based metric — **no external service, no DB,
free-tier only.**

## The log schema

```json
{"severity":"INFO",   "event":"gemini_parse","outcome":"success"}
{"severity":"WARNING","event":"gemini_parse","outcome":"fallback","reason":"transient_503"}
```

| field     | values |
|-----------|--------|
| `event`   | always `gemini_parse` (the stable filter key) |
| `outcome` | `success` \| `fallback` |
| `reason`  | present only on `fallback`: `transient_429` · `transient_503` · `no_api_key` · `bad_json` · `other` |

The `reason` buckets come from `src/llm._classify_error`, which is the **single
source of truth** shared with #33's retry decision (transient = `transient_429`/
`transient_503`) — so "what we retry" and "what we count as transient" can't drift.

A `fallback` means the message still landed as a **raw-text row** (nothing is lost);
the telemetry just tells us how often, and why, the structured parse degraded.

### ⚠️ What is *not* a fallback (until #9)

`outcome` measures **mechanical** failure only — Gemini erroring, refusing, or
returning unusable bytes. It says nothing about whether the *answer was any good*.

A blurry receipt, a photo of a mat, a screenshot, an off-topic text — none of these
log a `fallback`. Gemini is asked for JSON and dutifully returns well-formed JSON
with empty fields and `confidence: "low"`; `_generate_note` only checks
`isinstance(data, dict)` (`src/llm.py:307`), so it logs **`outcome="success"`** and a
junk row lands in the Sheet. The fallback rate stays clean while the ledger gets dirty.

So don't try to test the telemetry by sending a bad photo — you'll get a `success`
line. Fallbacks are **rare by design** (a real 429/503 after retries, a dead API key,
a blocked response); normal messages log `success`. That rarity is the point — it's
what the metric quantifies.

**Issue #9** closes this: it logs `confidence` on the success path so low-confidence
parses are countable, and flags those rows `NEEDS_REVIEW` for the owner.

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

`gcloud` equivalent:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision"
   AND resource.labels.service_name="catering-ledger-webhook"
   AND jsonPayload.event="gemini_parse" AND jsonPayload.outcome="fallback"' \
  --project=catering-ledger --freshness=24h \
  --format='table(timestamp, jsonPayload.reason)'
```

## Log-based metrics (fallback rate over time)

Two counter metrics give the **fallback rate** = `fallbacks / total`. Both are
plain, verified `gcloud` counters (no label-extractor gymnastics); the per-`reason`
split is read from `jsonPayload.reason` in Logs Explorer (above) or by adding a
label in the metric's console UI.

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

Then in **Metrics Explorer** chart `logging/user/gemini_parse_fallbacks` ÷
`logging/user/gemini_parse_total` (a ratio) for the fallback rate over any window.

> **⚠️ The ratio lies before both metrics existed.** Log-based metrics **do not
> backfill** — they only count lines written after the metric is created. The two
> here were created ~a day apart (`total` 2026-07-14 23:26 UTC, `fallbacks`
> 2026-07-15), so any window reaching back before **2026-07-15** has a structurally
> zero numerator over a real denominator and charts a **falsely perfect 0% fallback
> rate**. Only trust the ratio from 2026-07-15 forward.

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
