# Architecture & Data Contract

## Flow

1. **Ingest** — owner sends a photo / text / voice note to the WhatsApp number.
2. **Webhook** — Meta POSTs the message to the Cloud Function.
3. **Fetch media** — for photo/voice, the function downloads the media by ID
   using the WhatsApp access token.
4. **Parse** — Gemini Flash-Lite (free tier; model `gemini-flash-lite-latest`,
   an alias that tracks the current version so a retired pin can't break us;
   chosen over full Flash for higher free-tier quota / less throttling) converts
   the unstructured input into structured JSON against the fixed schema below. On
   low confidence, the row is flagged `NEEDS_REVIEW` rather than guessed.
   **A blank `amount` flags too** — a ledger row with no number is unusable
   however sure the model is, and it's what a non-receipt photo or a contentless
   text actually produces. The marker is prefixed to `Source/Notes` (never
   `Status`, which Tab B's `SUMIFS` read).
5. **Persist** — the row is appended to Tab A ("All Transactions").
6. **Confirm** — a human-readable reply is sent back (free, inside the 24h window).
   Only a **blank `amount`** earns a reply asking the owner to clarify. A row
   flagged merely for low confidence gets the ordinary `✅ Logged: …` reply, on
   purpose: a live smoke run (2026-07-15) returned a *complete, correct* row
   scoring `low` while a sparser row scored `high`, so `confidence` means "the
   model thinks it guessed", **not** "this row is junk". Every junk row is
   low-confidence, but not every low-confidence row is junk — asking her to
   re-send correct messages would train her to ignore the flag. The `NEEDS_REVIEW`
   marker is internal and is stripped before any reply.

## LLM output schema (the contract every slice reuses)

```json
{
  "date": "YYYY-MM-DD",
  "contract_name": "string (the client/account who books or pays)",
  "event": "string (the specific occasion, e.g. 'Diallo wedding')",
  "category": "Ingredients | Staff Salary | Revenue | ...",
  "type": "Expense | Revenue",
  "amount": 0.00,
  "notes": "string",
  "status": "Paid | Owed to us | Owed by us",
  "confidence": "high | low"
}
```

`contract_name` (the ongoing client) and `event` (the one-off occasion) are kept
**separate** so the Sheet can group either way. `status` is the payment-settlement
lifecycle: the model sets the initial value (defaulting to `Paid`), and the owner
flips a row to `Paid` by hand when the money actually arrives. Those three states
let one flat ledger answer **cash on hand** (`Paid`), **money owed to us**
(receivables — `Owed to us`), and **money we owe** (payables — `Owed by us`).

## Google Sheet layout

**Tab A — All Transactions** (append-only ledger)
`Date | Contract Name | Event | Type | Category | Amount | Source/Notes | Status`

**Tab B — Monthly Summary** (auditor dashboard)
- `B3` "Select Month" — defaults to `=TEXT(TODAY(),"mmmm yyyy")`, overridable.
- Hidden helper cells `X1`/`X2` — month start/end derived from `B3`.
- Per-category totals via `SUMIFS` filtered by Contract, Category, and the
  `X1..X2` date range. `SUMIFS` can also filter by `Status` to split settled cash
  from outstanding receivables/payables.
- **Note:** Tab A column letters shifted with the Event/Status columns above —
  any hard-coded column references in Tab B formulas must be updated to match.

## Security posture

- Auth to GCP from CI/CD is **keyless** (Workload Identity Federation).
- Runtime secrets (WhatsApp token, Gemini key) live in **Secret Manager**.
- Inbound webhooks are validated (verify token now; payload signature later).
