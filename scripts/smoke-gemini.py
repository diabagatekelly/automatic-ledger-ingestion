#!/usr/bin/env python
"""Live smoke test for Issue #4 — Gemini parses text notes into ledger rows.

Runs the real Gemini call (not mocked) against three sample notes and prints the
6-column row each would append. Use it to eyeball that the prompt produces
fully-populated, correct rows before/after deploy.

    export GEMINI_API_KEY=...            # from Google AI Studio
    python scripts/smoke-gemini.py

Requires GEMINI_API_KEY (and optionally GEMINI_MODEL). Makes real API calls
against the free tier; no Sheet is written.
"""

from __future__ import annotations

import os
import sys
from datetime import date

# Allow running from the repo root without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.llm import parse_note  # noqa: E402
from src.sheets import build_row, build_row_from_note  # noqa: E402

SAMPLES = [
    "Cash sale, $200, Wedding Cake",
    "Paid Amina 150 for kitchen help today",
    "Bought 40 dollars of flour and sugar for the Diallo baptism",
]

COLUMNS = ["Date", "Contract", "Category", "Type", "Amount", "Notes"]


def main() -> int:
    if not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: set GEMINI_API_KEY first (see .env.example).", file=sys.stderr)
        return 1

    today = date.today()
    ok = True
    for note in SAMPLES:
        print(f"\nNOTE: {note!r}")
        parsed = parse_note(note, today)
        if parsed is None:
            print("  parse FAILED -> falling back to raw-text row")
            row = build_row(note, today)
            ok = False
        else:
            print(f"  confidence={parsed.confidence}")
            row = build_row_from_note(parsed)
        for col, val in zip(COLUMNS, row, strict=True):
            print(f"    {col:9}: {val}")

    print("\nAll notes parsed." if ok else "\nSome notes fell back to raw text (see above).")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
