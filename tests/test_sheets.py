import dataclasses
from datetime import date
from unittest.mock import MagicMock

import pytest

from src.llm import ParsedNote
from src.sheets import (
    NEEDS_REVIEW,
    _mark_for_review,
    append_row,
    build_row,
    build_row_from_note,
    has_usable_amount,
    strip_review_marker,
)


def _note(**overrides: str) -> ParsedNote:
    """A clean, fully-populated note; override just the field under test."""
    base = {
        "date": "2026-07-13",
        "contract_name": "Diallo",
        "event": "Diallo wedding",
        "category": "Ingredients",
        "type": "Expense",
        "amount": "200",
        "notes": "Meat for the wedding",
        "confidence": "high",
        "status": "Paid",
    }
    return ParsedNote(**{**base, **overrides})  # type: ignore[arg-type]


# --- build_row (pure) ---


def test_build_row_places_date_and_notes_leaving_middle_blank() -> None:
    row = build_row("Cash sale, $200, Wedding Cake", date(2026, 7, 11))
    # 8 cols: Date | Contract | Event | Type | Category | Amount | Notes | Status.
    # Status is left blank on the raw fallback so an unparsed row is easy to spot.
    assert row == ["2026-07-11", "", "", "", "", "", "Cash sale, $200, Wedding Cake", ""]


def test_build_row_defuses_leading_formula_in_notes() -> None:
    row = build_row("=IMPORTXML(evil)", date(2026, 7, 11))
    assert row[6] == "'=IMPORTXML(evil)"


# --- NEEDS_REVIEW flagging (Issue #9) ---
#
# Two-tier rule (decided 2026-07-15). The Sheet flag follows ARCHITECTURE.md's
# "on low confidence, the row is flagged NEEDS_REVIEW rather than guessed", and a
# blank Amount flags too — a row with no number is unusable whatever the model
# claims about itself. The tiers differ only in the REPLY (see test_whatsapp):
# a blank Amount asks the owner to clarify; low-confidence-with-an-amount does not,
# because a live smoke run showed a correct row scoring low (see issue #9).


def test_build_row_from_note_flags_low_confidence_in_notes() -> None:
    row = build_row_from_note(_note(confidence="low"))
    assert row[6] == f"{NEEDS_REVIEW} — Meat for the wedding"


def test_build_row_from_note_flags_blank_amount_even_when_confident() -> None:
    # The model can be sure it found no amount. Still unusable.
    row = build_row_from_note(_note(amount="", confidence="high"))
    assert row[6].startswith(NEEDS_REVIEW)


def test_build_row_from_note_flags_whitespace_only_amount() -> None:
    row = build_row_from_note(_note(amount="   ", confidence="high"))
    assert row[6].startswith(NEEDS_REVIEW)


@pytest.mark.parametrize("amount", ["0", "0.0", "0.00", " 0 "])
def test_build_row_from_note_flags_a_zero_amount(amount: str) -> None:
    # Gemini does NOT reliably leave amount empty when it can't find one — a live
    # run on the contentless text "Recu paiement" returned amount=0 despite the
    # system instruction saying never to invent one. A zero-value catering entry
    # is meaningless, so treat it as no amount at all rather than trusting the
    # model to have obeyed. This is the exact case #9 exists to catch.
    row = build_row_from_note(_note(amount=amount, confidence="high"))
    assert row[6].startswith(NEEDS_REVIEW)


@pytest.mark.parametrize("amount", ["abc", "N/A", "-"])
def test_build_row_from_note_flags_an_unparseable_amount(amount: str) -> None:
    # coerce_note keeps a non-numeric string verbatim; it can't be a ledger amount.
    row = build_row_from_note(_note(amount=amount, confidence="high"))
    assert row[6].startswith(NEEDS_REVIEW)


@pytest.mark.parametrize("amount", ["200", "200.50", "0.01", "-50", "-0.01"])
def test_build_row_from_note_does_not_flag_a_real_amount(amount: str) -> None:
    # Negatives included deliberately: a refund or a correction is a real,
    # actionable figure. Only zero/blank/non-numeric mean "no amount".
    row = build_row_from_note(_note(amount=amount, confidence="high"))
    assert NEEDS_REVIEW not in row[6]


# --- has_usable_amount (pure) ---
#
# Tested directly, not only through its callers: both build_row_from_note and
# whatsapp.build_confirmation branch on it, so its edges are worth pinning once
# here rather than inferring them from row assertions.


@pytest.mark.parametrize("amount", ["200", "200.50", "0.01", "-50", " 200 "])
def test_has_usable_amount_accepts_any_non_zero_number(amount: str) -> None:
    assert has_usable_amount(amount) is True


@pytest.mark.parametrize(
    "amount",
    [
        "",
        "   ",
        "0",
        "0.0",
        "0.00",
        "-0",
        "-0.0",  # signed zero is still zero
        "abc",
        "N/A",
        "-",
        "200 CFA",  # a currency-tagged string isn't a number
    ],
)
def test_has_usable_amount_rejects_zero_blank_and_non_numeric(amount: str) -> None:
    assert has_usable_amount(amount) is False


@pytest.mark.parametrize("notes", ["Cash sale", "", "=IMPORTXML(evil)", "NEEDS_REVIEW literal"])
def test_review_marker_round_trips(notes: str) -> None:
    # The real guard against marking and stripping drifting apart: they share a
    # separator, and if one side's spelling changed the owner would silently start
    # seeing "NEEDS_REVIEW — " in her replies. Whatever we mark, we can unmark.
    assert strip_review_marker(_mark_for_review(notes)) == notes


def test_strip_review_marker_leaves_unmarked_notes_alone() -> None:
    assert strip_review_marker("Cash sale") == "Cash sale"


def test_build_row_from_note_does_not_flag_a_clean_row() -> None:
    row = build_row_from_note(_note())
    assert NEEDS_REVIEW not in row[6]
    assert row[6] == "Meat for the wedding"


def test_build_row_from_note_flags_with_no_notes_text() -> None:
    # No trailing separator when there's nothing to append it to.
    row = build_row_from_note(_note(notes="", confidence="low"))
    assert row[6] == NEEDS_REVIEW


def test_build_row_from_note_flag_does_not_defeat_formula_defusing() -> None:
    # The marker must not become a vehicle for injection: prefixing moves the
    # trigger off the front of the cell, which is exactly what makes it inert —
    # Sheets only evaluates a cell that STARTS with a trigger.
    row = build_row_from_note(_note(notes="=IMPORTXML(evil)", confidence="low"))
    assert row[6] == f"{NEEDS_REVIEW} — =IMPORTXML(evil)"
    assert not row[6].startswith(("=", "+", "-", "@"))


def test_build_row_from_note_flag_leaves_other_columns_untouched() -> None:
    # The flag rides in Notes only — Status carries the payment lifecycle and
    # Tab B's SUMIFS depend on it, so it must never be repurposed.
    row = build_row_from_note(_note(confidence="low"))
    assert row[7] == "Paid"
    assert row[5] == "200"


# --- build_row_from_note (pure) ---


def test_build_row_from_note_maps_every_column() -> None:
    # Distinct Type vs Category values prove the column order (Type before
    # Category), and Event/Status prove the two new columns land correctly.
    note = ParsedNote(
        date="2026-07-13",
        contract_name="Diallo",
        event="Diallo wedding",
        category="Ingredients",
        type="Expense",
        amount="200",
        notes="Meat for the wedding",
        confidence="high",
        status="Owed by us",
    )
    assert build_row_from_note(note) == [
        "2026-07-13",  # Date
        "Diallo",  # Contract Name
        "Diallo wedding",  # Event
        "Expense",  # Type
        "Ingredients",  # Category
        "200",  # Amount
        "Meat for the wedding",  # Source/Notes
        "Owed by us",  # Status
    ]


# Column order maps each ParsedNote field to its index in the appended row.
_FIELD_INDEX = {
    "date": 0,
    "contract_name": 1,
    "event": 2,
    "type": 3,
    "category": 4,
    "amount": 5,
    "notes": 6,
    "status": 7,
}
_BASE_NOTE = ParsedNote(
    date="2026-07-13",
    contract_name="Diallo",
    event="Diallo wedding",
    category="Revenue",
    type="Revenue",
    amount="200",
    notes="Cash sale",
    confidence="high",
    status="Paid",
)


@pytest.mark.parametrize("field", list(_FIELD_INDEX))
@pytest.mark.parametrize("trigger", ["=", "+", "-", "@"])
def test_build_row_from_note_defuses_every_trigger_in_every_text_cell(
    field: str, trigger: str
) -> None:
    # Every cell is text in the sheet, so a leading formula trigger in ANY
    # column (including Amount, e.g. "=1+2") must be prefixed with an apostrophe.
    value = f"{trigger}1+2" if field == "amount" else f"{trigger}evil"
    row = build_row_from_note(dataclasses.replace(_BASE_NOTE, **{field: value}))
    assert row[_FIELD_INDEX[field]] == f"'{value}"


# --- append_row (Sheets adapter, mocked client) ---


def test_append_row_appends_to_all_transactions_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHEET_ID", "sheet-123")
    service = MagicMock()
    monkeypatch.setattr("src.sheets._build_service", lambda: service)

    append_row(["2026-07-11", "", "", "", "", "", "Cash sale", ""])

    append = service.spreadsheets.return_value.values.return_value.append
    append.assert_called_once_with(
        spreadsheetId="sheet-123",
        range="All Transactions!A:H",
        valueInputOption="USER_ENTERED",
        body={"values": [["2026-07-11", "", "", "", "", "", "Cash sale", ""]]},
    )
    append.return_value.execute.assert_called_once_with()


def test_append_row_raises_clear_error_when_sheet_id_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SHEET_ID", raising=False)
    monkeypatch.setattr("src.sheets._build_service", MagicMock())

    with pytest.raises(RuntimeError, match="SHEET_ID"):
        append_row(["2026-07-11", "", "", "", "", "", "Cash sale", ""])
