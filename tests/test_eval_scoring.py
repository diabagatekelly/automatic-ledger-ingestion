"""Unit tests for the eval scorer (Issue #30).

The runner ``scripts/eval-gemini.py`` is a live, non-deterministic script
(validated by a real Gemini run like the other ``smoke-*`` scripts), but the
*scoring* it depends on is pure, deterministic logic — a buggy scorer produces a
meaningless baseline — so it lives in ``evals/scoring.py`` and is tested here.
"""

from __future__ import annotations

from evals.scoring import (
    SCORED_FIELDS,
    Scorecard,
    aggregate,
    amounts_equal,
    field_equal,
    format_scorecard,
    percentile,
    score_case,
)
from src.llm import ParsedNote


def _parsed(**overrides: str) -> ParsedNote:
    """A clean, fully-populated ParsedNote; override just the field under test."""
    base = {
        "date": "2026-07-13",
        "contract_name": "Diallo",
        "category": "Revenue",
        "type": "Revenue",
        "amount": "200",
        "notes": "Cash sale",
        "confidence": "high",
        "event": "",
        "status": "Paid",
    }
    base.update(overrides)
    return ParsedNote(**base)  # type: ignore[arg-type]


# --- amounts_equal -----------------------------------------------------------


def test_amounts_equal_numeric_forms_match() -> None:
    # "200" == "200.0" == "200" regardless of how the model stringifies it.
    assert amounts_equal("200", "200.0")
    assert amounts_equal("200.0", "200")
    assert amounts_equal("20000", "20000")


def test_amounts_equal_both_blank_matches() -> None:
    # A case with no amount, correctly left blank, is a match.
    assert amounts_equal("", "")


def test_amounts_equal_blank_vs_number_differs() -> None:
    assert not amounts_equal("", "0")
    assert not amounts_equal("200", "")


def test_amounts_equal_different_numbers_differ() -> None:
    assert not amounts_equal("200", "250")


def test_amounts_equal_non_numeric_falls_back_to_string() -> None:
    assert amounts_equal("200 CFA", "200 CFA")
    assert not amounts_equal("200 CFA", "200")


# --- field_equal -------------------------------------------------------------


def test_field_equal_text_is_case_and_space_insensitive() -> None:
    assert field_equal("category", "Revenue", " revenue ")
    assert field_equal("contract_name", "ONEP", "onep")


def test_field_equal_text_mismatch() -> None:
    assert not field_equal("type", "Revenue", "Expense")


def test_field_equal_amount_uses_numeric_compare() -> None:
    assert field_equal("amount", "200", "200.0")


# --- score_case --------------------------------------------------------------


def test_score_case_all_fields_correct_is_exact_match() -> None:
    expected = {
        "date": "2026-07-13",
        "contract_name": "Diallo",
        "category": "Revenue",
        "type": "Revenue",
        "amount": "200",
    }
    result = score_case("c1", expected, _parsed(), latency_s=0.4)
    assert result.fell_back is False
    assert all(result.field_correct[f] for f in SCORED_FIELDS)
    assert result.exact_match is True
    assert result.latency_s == 0.4


def test_score_case_one_wrong_field_is_not_exact_match() -> None:
    expected = {
        "date": "2026-07-13",
        "contract_name": "Diallo",
        "category": "Revenue",
        "type": "Revenue",
        "amount": "200",
    }
    result = score_case("c1", expected, _parsed(amount="250"), latency_s=0.4)
    assert result.field_correct["amount"] is False
    assert result.field_correct["category"] is True
    assert result.exact_match is False


def test_score_case_fallback_has_no_field_scores() -> None:
    expected = {"date": "2026-07-13", "amount": "200"}
    result = score_case("c1", expected, None, latency_s=1.2)
    assert result.fell_back is True
    assert result.field_correct == {}
    assert result.exact_match is False
    assert result.latency_s == 1.2


def test_score_case_confidence_checked_only_when_expected() -> None:
    expected = {
        "date": "2026-07-13",
        "contract_name": "",
        "category": "Revenue",
        "type": "Revenue",
        "amount": "",
    }
    # Ambiguous case: we expect the model to flag itself low.
    low = score_case(
        "c1",
        expected,
        _parsed(confidence="low", amount=""),
        latency_s=0.1,
        expected_confidence="low",
    )
    assert low.confidence_correct is True
    high = score_case(
        "c1",
        expected,
        _parsed(confidence="high", amount=""),
        latency_s=0.1,
        expected_confidence="low",
    )
    assert high.confidence_correct is False
    # No expected confidence -> not scored.
    none = score_case("c1", expected, _parsed(), latency_s=0.1)
    assert none.confidence_correct is None


# --- aggregate ---------------------------------------------------------------


def _expected(**overrides: str) -> dict[str, str]:
    base = {
        "date": "2026-07-13",
        "contract_name": "Diallo",
        "category": "Revenue",
        "type": "Revenue",
        "amount": "200",
    }
    base.update(overrides)
    return base


def test_aggregate_counts_and_rates() -> None:
    results = [
        score_case("ok", _expected(), _parsed(), latency_s=0.2),
        score_case("wrong-amt", _expected(), _parsed(amount="999"), latency_s=0.4),
        score_case("fell-back", _expected(), None, latency_s=0.6),
    ]
    card = aggregate(results, model="gemini-flash-lite-latest", run_date="2026-07-20")

    assert isinstance(card, Scorecard)
    assert card.total == 3
    assert card.fallbacks == 1
    assert card.parsed == 2
    # Exact match is over ALL cases (fallback counts as a miss): 1 of 3.
    assert card.overall_exact_match_rate == 1 / 3
    # Per-field accuracy is over PARSED cases only (2): amount 1/2, others 2/2.
    assert card.per_field_accuracy["amount"] == 1 / 2
    assert card.per_field_accuracy["category"] == 2 / 2
    assert card.fallback_rate == 1 / 3


def test_aggregate_confidence_accuracy_over_labelled_cases_only() -> None:
    results = [
        score_case(
            "amb1",
            _expected(amount=""),
            _parsed(amount="", confidence="low"),
            latency_s=0.1,
            expected_confidence="low",
        ),
        score_case(
            "amb2",
            _expected(amount=""),
            _parsed(amount="", confidence="high"),
            latency_s=0.1,
            expected_confidence="low",
        ),
        score_case("plain", _expected(), _parsed(), latency_s=0.1),
    ]
    card = aggregate(results, model="m", run_date="2026-07-20")
    # Only the two labelled cases count; 1 of 2 correct.
    assert card.confidence_labelled == 2
    assert card.confidence_accuracy == 1 / 2


def test_aggregate_all_fallbacks_has_no_per_field_denominator() -> None:
    results = [
        score_case("f1", _expected(), None, latency_s=0.1),
        score_case("f2", _expected(), None, latency_s=0.2),
    ]
    card = aggregate(results, model="m", run_date="2026-07-20")
    assert card.parsed == 0
    # No parsed cases -> per-field accuracy is undefined, reported as None.
    assert card.per_field_accuracy["amount"] is None
    assert card.overall_exact_match_rate == 0.0


def test_aggregate_confidence_excludes_labelled_fallbacks() -> None:
    # A labelled ambiguous case that FELL BACK has no confidence to judge, so it
    # must be excluded from the denominator — not counted as an incorrect guess.
    results = [
        score_case(
            "amb-parsed",
            _expected(amount=""),
            _parsed(amount="", confidence="low"),
            latency_s=0.1,
            expected_confidence="low",
        ),
        score_case(
            "amb-fellback", _expected(amount=""), None, latency_s=0.2, expected_confidence="low"
        ),
    ]
    card = aggregate(results, model="m", run_date="2026-07-20")
    # Only the parsed labelled case counts -> 1 of 1, not 1 of 2.
    assert card.confidence_labelled == 1
    assert card.confidence_accuracy == 1.0


# --- format_scorecard --------------------------------------------------------


def test_format_scorecard_includes_key_lines_and_confidence() -> None:
    results = [
        score_case("ok", _expected(), _parsed(), latency_s=0.2),
        # A parsed labelled case that misses on amount (expected blank, got "5")
        # -> not exact, so overall exact-match is 50%, but confidence is judged.
        score_case(
            "amb",
            _expected(amount=""),
            _parsed(amount="5", confidence="low"),
            latency_s=0.3,
            expected_confidence="low",
        ),
    ]
    text = format_scorecard(aggregate(results, model="gemini-x", run_date="2026-07-20"))
    assert "gemini-x" in text
    assert "2026-07-20" in text
    assert "overall exact-match: 50%" in text
    assert "fallback rate:       0%" in text
    assert "date           100%" in text
    # A labelled ambiguous case -> the confidence line is present.
    assert "confidence (ambiguous cases): 100%" in text
    assert "p50 0.20s" in text


def test_format_scorecard_shows_na_and_omits_confidence_when_none() -> None:
    # All fallbacks -> per-field is n/a, and with no labelled cases the
    # confidence line is omitted entirely.
    results = [score_case("f1", _expected(), None, latency_s=0.1)]
    text = format_scorecard(aggregate(results, model="m", run_date="2026-07-20"))
    assert "amount         n/a" in text
    assert "confidence (ambiguous cases)" not in text


# --- percentile --------------------------------------------------------------


def test_percentile_p50_and_p95() -> None:
    values = [0.1, 0.2, 0.3, 0.4, 0.5]
    assert percentile(values, 50) == 0.3
    # Nearest-rank p95 of 5 samples -> the top sample.
    assert percentile(values, 95) == 0.5


def test_percentile_empty_is_zero() -> None:
    assert percentile([], 50) == 0.0


def test_percentile_single_value() -> None:
    assert percentile([0.7], 95) == 0.7
