#!/usr/bin/env python
"""Live smoke test for Issue #5 — Gemini parses receipt photos into ledger rows.

Runs the real multimodal Gemini call (not mocked) against one or more local
image files and prints the 6-column row each would append. Use it to eyeball
that a snapped receipt produces a correct, fully-populated expense row before
wiring the WhatsApp media path end-to-end.

    export GEMINI_API_KEY=...            # from Google AI Studio
    python scripts/smoke-gemini-image.py path/to/receipt1.jpg receipt2.png ...

Requires GEMINI_API_KEY (and optionally GEMINI_MODEL). Makes real API calls
against the free tier; no Sheet is written and no WhatsApp download happens
(the bytes are read straight from disk, standing in for download_media).
"""

from __future__ import annotations

import mimetypes
import os
import sys
from datetime import date

# Allow running from the repo root without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.llm import parse_image  # noqa: E402
from src.sheets import build_row, build_row_from_note  # noqa: E402

COLUMNS = ["Date", "Contract", "Category", "Type", "Amount", "Notes"]


def _mime_type(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "image/jpeg"


def main(paths: list[str]) -> int:
    if not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: set GEMINI_API_KEY first (see .env.example).", file=sys.stderr)
        return 1
    if not paths:
        print("Usage: python scripts/smoke-gemini-image.py <image> [<image> ...]", file=sys.stderr)
        return 1

    today = date.today()
    ok = True
    for path in paths:
        print(f"\nRECEIPT: {path}")
        try:
            with open(path, "rb") as fh:
                image_bytes = fh.read()
        except OSError as exc:
            print(f"  could not read file: {exc}")
            ok = False
            continue
        mime_type = _mime_type(path)
        parsed = parse_image(image_bytes, mime_type, today)
        if parsed is None:
            print("  parse FAILED -> would fall back to a marker/caption row")
            row = build_row("[receipt photo received — could not read it automatically]", today)
            ok = False
        else:
            print(f"  mime={mime_type}  confidence={parsed.confidence}")
            row = build_row_from_note(parsed)
        for col, val in zip(COLUMNS, row, strict=True):
            print(f"    {col:9}: {val}")

    print("\nAll receipts parsed." if ok else "\nSome receipts fell back (see above).")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
