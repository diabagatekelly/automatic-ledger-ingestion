from datetime import date
from unittest.mock import MagicMock

import pytest

from src.llm import ParsedNote
from src.sheets import append_row, build_row, build_row_from_note

# --- build_row (pure) ---


def test_build_row_places_date_and_notes_leaving_middle_blank() -> None:
    row = build_row("Cash sale, $200, Wedding Cake", date(2026, 7, 11))
    assert row == ["2026-07-11", "", "", "", "", "Cash sale, $200, Wedding Cake"]


def test_build_row_defuses_leading_formula_in_notes() -> None:
    row = build_row("=IMPORTXML(evil)", date(2026, 7, 11))
    assert row[5] == "'=IMPORTXML(evil)"


# --- build_row_from_note (pure) ---


def test_build_row_from_note_maps_every_column() -> None:
    note = ParsedNote(
        date="2026-07-13",
        contract_name="Wedding Cake",
        category="Revenue",
        type="Revenue",
        amount="200",
        notes="Cash sale",
        confidence="high",
    )
    assert build_row_from_note(note) == [
        "2026-07-13",
        "Wedding Cake",
        "Revenue",
        "Revenue",
        "200",
        "Cash sale",
    ]


def test_build_row_from_note_defuses_formula_injection_in_text_fields() -> None:
    note = ParsedNote(
        date="2026-07-13",
        contract_name="=HYPERLINK(evil)",
        category="Revenue",
        type="Revenue",
        amount="200",
        notes="@SUM(A1:A9)",
        confidence="high",
    )
    row = build_row_from_note(note)
    assert row[1] == "'=HYPERLINK(evil)"  # Contract Name
    assert row[5] == "'@SUM(A1:A9)"  # Source/Notes


# --- append_row (Sheets adapter, mocked client) ---


def test_append_row_appends_to_all_transactions_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHEET_ID", "sheet-123")
    service = MagicMock()
    monkeypatch.setattr("src.sheets._build_service", lambda: service)

    append_row(["2026-07-11", "", "", "", "", "Cash sale"])

    append = service.spreadsheets.return_value.values.return_value.append
    append.assert_called_once_with(
        spreadsheetId="sheet-123",
        range="All Transactions!A:F",
        valueInputOption="USER_ENTERED",
        body={"values": [["2026-07-11", "", "", "", "", "Cash sale"]]},
    )
    append.return_value.execute.assert_called_once_with()


def test_append_row_raises_clear_error_when_sheet_id_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SHEET_ID", raising=False)
    monkeypatch.setattr("src.sheets._build_service", MagicMock())

    with pytest.raises(RuntimeError, match="SHEET_ID"):
        append_row(["2026-07-11", "", "", "", "", "Cash sale"])
