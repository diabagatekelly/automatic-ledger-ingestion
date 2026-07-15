"""Google Sheets adapter for the automatic-ledger-ingestion pipeline.

`build_row` is pure domain logic (no I/O) and is unit-tested directly.
`append_row` is a thin adapter over the Sheets API and is tested with a
mocked client — no live calls in CI.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import google.auth
from googleapiclient.discovery import build as discovery_build

from src.llm import ParsedNote, review_reason

_SHEET_RANGE = "All Transactions!A:H"
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_FORMULA_TRIGGERS = ("=", "+", "-", "@")

# Marker prefixed to the Source/Notes cell of a row the owner should eyeball
# (Issue #9). Notes and NOT Status: Status carries the payment lifecycle that Tab B's
# SUMIFS read, so repurposing it would corrupt the cash/receivable/payable math.
NEEDS_REVIEW = "NEEDS_REVIEW"
_REVIEW_SEPARATOR = " — "


def _mark_for_review(notes: str) -> str:
    """Prefix the review marker onto a Notes cell (inverse of strip_review_marker)."""
    return f"{NEEDS_REVIEW}{_REVIEW_SEPARATOR}{notes}" if notes else NEEDS_REVIEW


def strip_review_marker(notes: str) -> str:
    """Remove the review marker from a Notes cell, for display back to the owner.

    Lives here, beside ``_mark_for_review``, because the two are inverses and must
    agree on the exact prefix — including the separator. Splitting them across
    modules (the marker here, the separator re-spelled in the WhatsApp adapter)
    would let a change to one silently stop matching the other, and the failure
    would be invisible: the owner would just start seeing "NEEDS_REVIEW — " in her
    replies. ``whatsapp.build_confirmation`` imports this rather than reimplementing
    it. The round-trip is pinned by a test.
    """
    if notes == NEEDS_REVIEW:
        return ""
    prefix = f"{NEEDS_REVIEW}{_REVIEW_SEPARATOR}"
    return notes[len(prefix) :] if notes.startswith(prefix) else notes


def _defuse_formula(text: str) -> str:
    """Neutralize spreadsheet formula injection.

    With ``valueInputOption=USER_ENTERED`` a cell beginning with a formula
    trigger (``= + - @``) is evaluated as a formula. Prefixing an apostrophe
    forces Sheets to treat the value as literal text.
    """
    if text.startswith(_FORMULA_TRIGGERS):
        return "'" + text
    return text


def build_row(text: str, today: date) -> list[str]:
    """Build an 8-column ledger row from a raw text payload.

    Columns: Date | Contract Name | Event | Type | Category | Amount |
    Source/Notes | Status. For this fallback only Date and Source/Notes are
    populated; Status is left blank so an unparsed row is easy to spot and
    triage by hand.
    """
    return [today.isoformat(), "", "", "", "", "", _defuse_formula(text), ""]


def build_row_from_note(note: ParsedNote) -> list[str]:
    """Build an 8-column ledger row from a Gemini-parsed note.

    Columns: Date | Contract Name | Event | Type | Category | Amount |
    Source/Notes | Status. Text fields are defused against formula injection
    (the LLM echoes owner input, so its output is untrusted for spreadsheet
    purposes). A row needing the owner's eye gets NEEDS_REVIEW prefixed to Notes.
    """
    notes = note.notes
    # Same call _generate_note makes when it logs the outcome, so a row flagged
    # here is exactly a row logged needs_review — one rule, no drift.
    if review_reason(note) is not None:
        # Mark, then defuse the WHOLE cell: a marked "=IMPORTXML(evil)" is inert
        # precisely because the trigger no longer starts the cell, and defusing
        # first would leave a stray apostrophe mid-string.
        notes = _mark_for_review(notes)
    return [
        _defuse_formula(note.date),
        _defuse_formula(note.contract_name),
        _defuse_formula(note.event),
        _defuse_formula(note.type),
        _defuse_formula(note.category),
        _defuse_formula(note.amount),
        _defuse_formula(notes),
        _defuse_formula(note.status),
    ]


def _build_service() -> Any:
    """Build an authenticated Sheets API client.

    Auth via Application Default Credentials: locally this picks up
    ``GOOGLE_APPLICATION_CREDENTIALS``; in the cloud it uses the function's
    runtime service account.
    """
    credentials, _ = google.auth.default(scopes=_SCOPES)
    return discovery_build("sheets", "v4", credentials=credentials, cache_discovery=False)


def _sheet_id() -> str:
    """Return the configured Sheet ID, failing fast with a clear message."""
    sheet_id = os.getenv("SHEET_ID")
    if not sheet_id:
        raise RuntimeError("SHEET_ID environment variable is not set")
    return sheet_id


def append_row(row: list[str]) -> None:
    """Append one row to the 'All Transactions' tab of the configured Sheet."""
    service = _build_service()
    service.spreadsheets().values().append(
        spreadsheetId=_sheet_id(),
        range=_SHEET_RANGE,
        valueInputOption="USER_ENTERED",
        body={"values": [row]},
    ).execute()
