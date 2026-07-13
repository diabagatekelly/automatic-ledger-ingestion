# Architecture & Data Contract

## Flow

1. **Ingest** — owner sends a photo / text / voice note to the WhatsApp number.
2. **Webhook** — Meta POSTs the message to the Cloud Function.
3. **Fetch media** — for photo/voice, the function downloads the media by ID
   using the WhatsApp access token.
4. **Parse** — Gemini Flash (free tier; model `gemini-flash-latest`, an alias
   that tracks the current version so a retired pin can't break us) converts the
   unstructured input into structured JSON against the fixed schema below. On low
   confidence, the row is flagged `NEEDS_REVIEW` rather than guessed.
5. **Persist** — the row is appended to Tab A ("All Transactions").
6. **Confirm** — a human-readable reply is sent back (free, inside the 24h window).

## LLM output schema (the contract every slice reuses)

```json
{
  "date": "YYYY-MM-DD",
  "contract_name": "string",
  "category": "Ingredients | Staff Salary | Revenue | ...",
  "type": "Expense | Revenue",
  "amount": 0.00,
  "notes": "string",
  "confidence": "high | low"
}
```

## Google Sheet layout

**Tab A — All Transactions** (append-only ledger)
`Date | Contract Name | Category | Type | Amount | Source/Notes`

**Tab B — Monthly Summary** (auditor dashboard)
- `B3` "Select Month" — defaults to `=TEXT(TODAY(),"mmmm yyyy")`, overridable.
- Hidden helper cells `X1`/`X2` — month start/end derived from `B3`.
- Per-category totals via `SUMIFS` filtered by Contract, Category, and the
  `X1..X2` date range.

## Security posture

- Auth to GCP from CI/CD is **keyless** (Workload Identity Federation).
- Runtime secrets (WhatsApp token, Gemini key) live in **Secret Manager**.
- Inbound webhooks are validated (verify token now; payload signature later).
