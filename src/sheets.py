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

from src.llm import ParsedNote

_SHEET_RANGE = "All Transactions!A:H"
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_FORMULA_TRIGGERS = ("=", "+", "-", "@")

# Marker prefixed to the Source/Notes cell of a row the owner should eyeball
# (Issue #9). Part of the row contract, so it lives here with build_row_from_note
# and is imported (not re-spelled) by whatsapp.build_confirmation, which strips it
# before the owner ever sees it — two spellings of this string would silently stop
# matching. Notes and NOT Status: Status carries the payment lifecycle that Tab B's
# SUMIFS read, so repurposing it would corrupt the cash/receivable/payable math.
NEEDS_REVIEW = "NEEDS_REVIEW"


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


def has_usable_amount(amount: str) -> bool:
    """Whether an Amount cell holds a real, actionable figure.

    Blank, non-numeric ("abc", "N/A") and **zero** all mean "no amount". Zero is
    included because the model does NOT reliably leave the field empty when it
    finds nothing: a live run on the contentless text "Recu paiement" returned
    ``amount: 0`` despite the system instruction saying never to invent one, which
    would otherwise sail through as a confident "✅ Logged: Revenue 0". A
    zero-value catering entry carries no financial meaning, so treating it as
    absent costs nothing and stops a fabricated number reaching the ledger.

    Shared by ``_needs_review`` (flags the row) and ``whatsapp.build_confirmation``
    (asks the owner to clarify) so the Sheet and the reply can't disagree about
    whether a row is usable.
    """
    try:
        return float(amount.strip()) != 0
    except ValueError:
        return False


def _needs_review(note: ParsedNote) -> bool:
    """Whether a parsed row should be flagged for the owner to eyeball (#9).

    Two independent triggers:

    * ``confidence == "low"`` — ARCHITECTURE.md's rule ("on low confidence, the
      row is flagged NEEDS_REVIEW rather than guessed").
    * no usable ``amount`` — a ledger row with no number is unusable no matter how
      sure the model is, and this is what a photo of a mat or a contentless
      "Recu paiement" actually produces.

    They are deliberately separate: a 2026-07-15 smoke run against real Gemini
    returned a complete, correct row scoring ``low`` (and a sparser row scoring
    ``high``), so confidence alone is a weak signal — it means "the model thinks
    it guessed", not "this row is junk". The amount case is the one that is
    unambiguously broken, which is why only IT earns a clarifying reply; see
    whatsapp.build_confirmation and issue #9.
    """
    return note.confidence == "low" or not has_usable_amount(note.amount)


def build_row_from_note(note: ParsedNote) -> list[str]:
    """Build an 8-column ledger row from a Gemini-parsed note.

    Columns: Date | Contract Name | Event | Type | Category | Amount |
    Source/Notes | Status. Text fields are defused against formula injection
    (the LLM echoes owner input, so its output is untrusted for spreadsheet
    purposes). A row needing the owner's eye gets NEEDS_REVIEW prefixed to Notes.
    """
    notes = note.notes
    if _needs_review(note):
        # Prefix, then defuse the WHOLE cell: a marked "=IMPORTXML(evil)" is
        # inert precisely because the trigger no longer starts the cell, and
        # defusing first would leave a stray apostrophe mid-string.
        notes = f"{NEEDS_REVIEW} — {notes}" if notes else NEEDS_REVIEW
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
