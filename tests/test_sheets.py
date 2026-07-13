import dataclasses
from datetime import date
from unittest.mock import MagicMock

import pytest

from src.llm import ParsedNote
from src.sheets import append_row, build_row, build_row_from_note

# --- build_row (pure) ---


def test_build_row_places_date_and_notes_leaving_middle_blank() -> None:
    row = build_row("Cash sale, $200, Wedding Cake", date(2026, 7, 11))
    # 8 cols: Date | Contract | Event | Type | Category | Amount | Notes | Status.
    # Status is left blank on the raw fallback so an unparsed row is easy to spot.
    assert row == ["2026-07-11", "", "", "", "", "", "Cash sale, $200, Wedding Cake", ""]


def test_build_row_defuses_leading_formula_in_notes() -> None:
    row = build_row("=IMPORTXML(evil)", date(2026, 7, 11))
    assert row[6] == "'=IMPORTXML(evil)"


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
