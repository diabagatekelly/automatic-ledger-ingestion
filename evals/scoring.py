"""Pure scoring for the Gemini accuracy eval (Issue #30).

Deterministic, dependency-light logic that turns a parsed row + its expected
labels into a per-field / overall / fallback / latency scorecard. Kept apart
from the live runner (``scripts/eval-gemini.py``) so a buggy scorer can't quietly
poison the committed baseline — this is the part with real edge cases (numeric
amount forms, blank-vs-zero, case-folding), so this is the part with tests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from src.llm import ParsedNote

# The fields we score for accuracy — the ones that actually land in the ledger
# and that the model has to get right. ``notes`` is free prose (not scorable),
# ``status``/``event`` are frequently absent from short notes; ``confidence`` is
# scored separately, only on cases we deliberately labelled as ambiguous.
SCORED_FIELDS: tuple[str, ...] = ("date", "contract_name", "category", "type", "amount")


def amounts_equal(expected: str, actual: str) -> bool:
    """Whether two amount cells mean the same figure.

    Compares numerically so the model's stringification doesn't matter ("200" ==
    "200.0"), but keeps a blank distinct from any number (a missing amount is a
    different outcome from a zero). Non-numeric values ("200 CFA") fall back to an
    exact string compare.
    """
    exp, act = expected.strip(), actual.strip()
    if exp == act:
        return True
    try:
        return float(exp) == float(act)
    except ValueError:
        return False


def field_equal(name: str, expected: str, actual: str) -> bool:
    """Whether a single scored field matches its expected label.

    Amounts compare numerically (see ``amounts_equal``); every other field is a
    case- and whitespace-insensitive string match, so "revenue" == "Revenue".
    """
    if name == "amount":
        return amounts_equal(expected, actual)
    return expected.strip().casefold() == actual.strip().casefold()


@dataclass(frozen=True)
class CaseResult:
    """The scored outcome of one eval case."""

    case_id: str
    fell_back: bool
    field_correct: dict[str, bool]
    exact_match: bool
    latency_s: float
    confidence_expected: str | None = None
    confidence_actual: str | None = None
    confidence_correct: bool | None = None


def score_case(
    case_id: str,
    expected: dict[str, str],
    parsed: ParsedNote | None,
    latency_s: float,
    expected_confidence: str | None = None,
) -> CaseResult:
    """Score one parsed row (or fallback) against its expected labels.

    A ``None`` parse is a **fallback** — the model/infra failed to return a usable
    row — and is scored with no per-field credit and ``exact_match=False``, so it
    counts against the overall rate while staying separable from wrong answers in
    the aggregate. ``expected_confidence`` is only set for deliberately-ambiguous
    cases; when set, we also check the model flagged itself accordingly.
    """
    if parsed is None:
        return CaseResult(
            case_id=case_id,
            fell_back=True,
            field_correct={},
            exact_match=False,
            latency_s=latency_s,
            confidence_expected=expected_confidence,
        )

    actual = {
        "date": parsed.date,
        "contract_name": parsed.contract_name,
        "category": parsed.category,
        "type": parsed.type,
        "amount": parsed.amount,
    }
    field_correct = {
        name: field_equal(name, expected.get(name, ""), actual[name]) for name in SCORED_FIELDS
    }
    confidence_correct = (
        parsed.confidence == expected_confidence if expected_confidence is not None else None
    )
    return CaseResult(
        case_id=case_id,
        fell_back=False,
        field_correct=field_correct,
        exact_match=all(field_correct.values()),
        latency_s=latency_s,
        confidence_expected=expected_confidence,
        confidence_actual=parsed.confidence,
        confidence_correct=confidence_correct,
    )


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile of ``values`` (0.0 for an empty list).

    Nearest-rank (not interpolated) because the eval sample is small — a handful
    of latencies — where interpolation implies a precision we don't have.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = math.ceil(pct / 100 * len(ordered))
    index = min(max(rank, 1), len(ordered)) - 1
    return ordered[index]


@dataclass(frozen=True)
class Scorecard:
    """Aggregate accuracy/latency across every eval case."""

    model: str
    run_date: str
    total: int
    parsed: int
    fallbacks: int
    fallback_rate: float
    overall_exact_match_rate: float
    per_field_accuracy: dict[str, float | None]
    latency_p50: float
    latency_p95: float
    confidence_labelled: int
    confidence_accuracy: float | None
    results: list[CaseResult] = field(default_factory=list)


def aggregate(results: list[CaseResult], *, model: str, run_date: str) -> Scorecard:
    """Roll per-case results into a single scorecard.

    Denominators matter and are chosen deliberately:
    - **per-field accuracy** is over PARSED cases only, so an infra fallback can't
      masquerade as a field the model got wrong (``None`` when nothing parsed).
    - **overall exact-match** and **fallback rate** are over ALL cases — a
      fallback is a real miss for the owner, just not a *wrong-answer* miss.
    - **confidence accuracy** is over only the cases we labelled as ambiguous.
    """
    total = len(results)
    parsed_results = [r for r in results if not r.fell_back]
    parsed = len(parsed_results)
    fallbacks = total - parsed

    per_field_accuracy: dict[str, float | None] = {}
    for name in SCORED_FIELDS:
        if parsed == 0:
            per_field_accuracy[name] = None
        else:
            correct = sum(1 for r in parsed_results if r.field_correct[name])
            per_field_accuracy[name] = correct / parsed

    exact_matches = sum(1 for r in results if r.exact_match)
    latencies = [r.latency_s for r in results]

    # Only PARSED ambiguous cases: a labelled case that fell back has
    # confidence_correct=None (no confidence was produced), and counting that as
    # an incorrect prediction would blame the model for an infra fallback.
    labelled = [r for r in results if r.confidence_expected is not None and not r.fell_back]
    confidence_accuracy: float | None = None
    if labelled:
        confidence_accuracy = sum(1 for r in labelled if r.confidence_correct) / len(labelled)

    return Scorecard(
        model=model,
        run_date=run_date,
        total=total,
        parsed=parsed,
        fallbacks=fallbacks,
        fallback_rate=fallbacks / total if total else 0.0,
        overall_exact_match_rate=exact_matches / total if total else 0.0,
        per_field_accuracy=per_field_accuracy,
        latency_p50=percentile(latencies, 50),
        latency_p95=percentile(latencies, 95),
        confidence_labelled=len(labelled),
        confidence_accuracy=confidence_accuracy,
        results=results,
    )


def _pct(value: float | None) -> str:
    """Format a 0..1 ratio as a percentage, or ``n/a`` when undefined."""
    return "n/a" if value is None else f"{value * 100:.0f}%"


def format_scorecard(card: Scorecard) -> str:
    """Render a human-readable scorecard block for the console + the baseline doc."""
    lines = [
        f"Gemini parse eval - model {card.model} - {card.run_date}",
        f"  cases: {card.total}  (parsed {card.parsed}, fallback {card.fallbacks})",
        f"  overall exact-match: {_pct(card.overall_exact_match_rate)}",
        f"  fallback rate:       {_pct(card.fallback_rate)}",
        "  per-field accuracy (over parsed cases):",
    ]
    for name in SCORED_FIELDS:
        lines.append(f"    {name:<14} {_pct(card.per_field_accuracy[name])}")
    if card.confidence_labelled:
        lines.append(
            f"  confidence (ambiguous cases): {_pct(card.confidence_accuracy)}"
            f"  ({card.confidence_labelled} labelled)"
        )
    lines.append(f"  latency: p50 {card.latency_p50:.2f}s  p95 {card.latency_p95:.2f}s")
    return "\n".join(lines)
