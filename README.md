# catering-ledger

Automated, near-zero-cost accounting pipeline for a small catering business.

The owner sends a **photo of a receipt** or a **text/voice note** for a cash sale
over WhatsApp. A serverless webhook parses the unstructured input into structured
rows with a multimodal LLM and appends them to a Google Sheet that doubles as the
database, the owner's UI, and the auditor's dashboard.

> Design principle: **fire-and-forget** for the owner, **near-zero cost/maintenance**
> for the developer.

## Architecture

```
 WhatsApp (photo / text / voice)
        │  inbound webhook
        ▼
 Cloud Function (Gen2, Python)  ──►  Gemini 2.5 Flash  (parse → structured JSON)
        │                                   │
        │  append row                       │  confirmation reply (free, in 24h window)
        ▼                                   ▼
 Google Sheet  ── Tab A: All Transactions (ledger)
                └ Tab B: Monthly Summary (SUMIFS dashboard)
```

## Stack

| Concern      | Choice                                   | Why |
|--------------|------------------------------------------|-----|
| Compute      | GCP Cloud Functions (Gen2)               | Real serverless skills, low maintenance, generous free tier |
| Language     | Python 3.12                              | Terse for Google APIs + Gemini |
| Parsing      | Gemini 2.5 Flash (multimodal)            | Free tier covers image + text + audio; ~250 req/day free |
| Ingestion    | WhatsApp Cloud API                       | Owner already uses it; inbound + service replies are free |
| Storage / UI | Google Sheets                            | Free; single source of truth *and* the owner-facing UI |
| CI / CD      | GitHub Actions (keyless deploy via WIF)  | No stored service-account keys; native to the repo |

## Development

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env        # then fill in secrets — .env is git-ignored

# Quality gate (same as CI)
ruff check . && black --check . && mypy src && pytest

# Run the function locally
functions-framework --target=webhook --debug
```

## Deployment

CD runs on GitHub Actions and authenticates to GCP with **Workload Identity
Federation** — no service-account JSON is ever stored. See `.github/workflows/deploy.yml`.
It is `workflow_dispatch` (manual) until WIF is provisioned, then flips to deploy on
merge to `main`.

## Security

- Secrets live in env / GCP Secret Manager — **never** in the repo.
- `.gitignore` blocks `.env`, keys, and service-account JSON.
- GitHub secret scanning + push protection are enabled on this repo.

## Roadmap

Work is tracked as **thin vertical slices** — each issue is demoable end to end.
See the [project board](../../projects) and [issues](../../issues).

## License

MIT — see [LICENSE](LICENSE).
