"""Google Sheets adapter for the catering-ledger.

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

_SHEET_RANGE = "All Transactions!A:F"
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def build_row(text: str, today: date) -> list[str]:
    """Build a 6-column ledger row from a raw text payload.

    Columns: Date | Contract Name | Category | Type | Amount | Source/Notes.
    For this slice only Date and Source/Notes are populated.
    """
    return [today.isoformat(), "", "", "", "", text]


def _build_service() -> Any:
    """Build an authenticated Sheets API client.

    Auth via Application Default Credentials: locally this picks up
    ``GOOGLE_APPLICATION_CREDENTIALS``; in the cloud it uses the function's
    runtime service account.
    """
    credentials, _ = google.auth.default(scopes=_SCOPES)
    return discovery_build("sheets", "v4", credentials=credentials, cache_discovery=False)


def append_row(row: list[str]) -> None:
    """Append one row to the 'All Transactions' tab of the configured Sheet."""
    service = _build_service()
    service.spreadsheets().values().append(
        spreadsheetId=os.environ["SHEET_ID"],
        range=_SHEET_RANGE,
        valueInputOption="USER_ENTERED",
        body={"values": [row]},
    ).execute()
