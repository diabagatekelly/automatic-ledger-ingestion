#!/usr/bin/env python
"""Live accuracy eval for the Gemini parse (Issue #30).

Runs ``src/llm.py``'s real parse over the labeled dataset in
``evals/dataset.jsonl`` and prints a scored scorecard: per-field accuracy,
overall exact-match, fallback rate (reported separately from wrong answers), and
latency p50/p95. Unlike the unit tests — which score OUR coercion against a
mocked Gemini — this scores the **model + prompt** against real inputs, so it
catches the class of regression that only shows up live (a retired model, a
prompt tweak that quietly worsens a field). Commit the printed block as the
baseline so future prompt/model changes are measured against a known number.

    export GEMINI_API_KEY=...            # from Google AI Studio
    python scripts/eval-gemini.py                       # full dataset
    python scripts/eval-gemini.py --limit 3             # quick smoke
    python scripts/eval-gemini.py --dataset other.jsonl

Makes real, quota-costing API calls against the free tier; writes no Sheet. Not
part of CI (nondeterministic + costs quota).

Reproducibility: every case is parsed against a FIXED reference date (default
``2026-07-20``), NOT ``date.today()`` — otherwise the "no date stated -> use
today" and relative-date ("yesterday") cases would drift day to day and their
expected labels would rot. The dataset's expected dates are resolved against
``--reference-date``; change one and you must change the other.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
from datetime import date

# Allow running from the repo root without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evals.scoring import aggregate, format_scorecard, score_case  # noqa: E402
from src.llm import ParsedNote, parse_image, parse_note  # noqa: E402

_DEFAULT_DATASET = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "evals", "dataset.jsonl"
)
_REFERENCE_DATE = "2026-07-20"


def _load_cases(path: str) -> list[dict]:
    """Read the JSONL dataset into a list of case dicts (blank lines skipped)."""
    cases = []
    with open(path, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_no}: invalid JSON — {exc}") from exc
    return cases


def _resolve_image_path(case: dict) -> str:
    """Absolute path to an image case's file (relative paths are repo-root based)."""
    image_path = case["image"]
    if not os.path.isabs(image_path):
        image_path = os.path.join(os.path.dirname(_DEFAULT_DATASET), "..", image_path)
    return image_path


def _run_case(case: dict, today: date) -> tuple[ParsedNote | None, float]:
    """Parse one case (text or image) against Gemini; return (parsed, latency_s).

    ``parse_note``/``parse_image`` swallow their own failures and return ``None``
    (the raw-text fallback), which the scorer counts as a fallback — so infra
    flakiness stays separable from wrong answers. ``parse_note`` already retries
    transient 429/503 internally (#33), so a persistent fallback here is a genuine
    failure, not a one-off blip.
    """
    kind = case.get("kind", "text")
    started = time.perf_counter()
    if kind == "image":
        with open(_resolve_image_path(case), "rb") as handle:
            image_bytes = handle.read()
        mime_type = mimetypes.guess_type(case["image"])[0] or "image/jpeg"
        parsed = parse_image(image_bytes, mime_type, today, caption=case.get("caption", ""))
    else:
        parsed = parse_note(case["text"], today)
    return parsed, time.perf_counter() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live Gemini accuracy eval (Issue #30).")
    parser.add_argument("--dataset", default=_DEFAULT_DATASET, help="Path to the JSONL dataset.")
    parser.add_argument(
        "--reference-date", default=_REFERENCE_DATE, help="Fixed 'today' for parses."
    )
    parser.add_argument("--limit", type=int, default=None, help="Only run the first N cases.")
    parser.add_argument(
        "--sleep",
        type=float,
        default=4.0,
        help=(
            "Seconds to pace between calls. The free tier allows ~15 requests/min, "
            "so the default (4s) keeps a full run under the limit; a 429 there would "
            "otherwise fall back and pollute the score. Set 0 on a paid key."
        ),
    )
    args = parser.parse_args(argv)

    if not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: set GEMINI_API_KEY first (see .env.example).", file=sys.stderr)
        return 1

    today = date.fromisoformat(args.reference_date)
    cases = _load_cases(args.dataset)
    if args.limit is not None:
        cases = cases[: args.limit]
    if not cases:
        print("ERROR: dataset has no cases.", file=sys.stderr)
        return 1

    # Image cases point at real receipts kept OUT of the public repo (PII); when
    # they're absent locally, skip them so the text half still runs and stays
    # reproducible for anyone cloning — rather than crashing the whole eval.
    runnable = []
    for case in cases:
        if case.get("kind") == "image" and not os.path.exists(_resolve_image_path(case)):
            print(f"  {case['id']:<28} SKIPPED   (image not found locally)")
            continue
        runnable.append(case)
    if not runnable:
        print("ERROR: no runnable cases (all image files missing?).", file=sys.stderr)
        return 1

    results = []
    for index, case in enumerate(runnable):
        if index and args.sleep:
            time.sleep(args.sleep)  # pace under the free-tier RPM (see --sleep)
        parsed, latency_s = _run_case(case, today)
        result = score_case(
            case["id"],
            case["expected"],
            parsed,
            latency_s,
            expected_confidence=case.get("expected_confidence"),
        )
        results.append(result)
        mark = "FALLBACK" if result.fell_back else ("exact" if result.exact_match else "miss")
        print(f"  {case['id']:<28} {mark:<9} {latency_s:.2f}s")
        if not result.fell_back and not result.exact_match:
            wrong = [f for f, ok in result.field_correct.items() if not ok]
            print(f"      wrong fields: {', '.join(wrong)}")

    card = aggregate(
        results,
        model=os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest"),
        run_date=today.isoformat(),
    )
    print("\n" + format_scorecard(card))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
