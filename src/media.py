"""WhatsApp Cloud API media download adapter.

Inbound image/audio messages carry only a media *ID*, not the bytes. Fetching
the bytes is a two-step dance against the Graph API, both calls authenticated
with the WhatsApp access token:

  1. GET ``/{media-id}``            → JSON with a short-lived ``url`` + ``mime_type``
  2. GET that ``url``               → the raw media bytes

Thin I/O adapter (like ``src.sheets.append_row``); tested with a mocked
``requests`` — no live calls in CI.
"""

from __future__ import annotations

import os

import requests

from src.graph import GRAPH_API_BASE, TIMEOUT_SECONDS
from src.telemetry import log_event

# Media-download failure telemetry (Issue #44). A failed download never reaches
# Gemini, so it is INVISIBLE to the gemini_parse telemetry — the #5 expired-token
# incident filled the Sheet with unreadable-image rows while every parse
# dashboard stayed green. This event makes that failure mode queryable.
# ``auth_401`` is split out deliberately: it is the token-expiry signature and
# the single highest-value thing to notice.
_EVENT_MEDIA_DOWNLOAD = "media_download"
_OUTCOME_FAILURE = "failure"
_REASON_AUTH_401 = "auth_401"
_REASON_NOT_FOUND = "not_found"
_REASON_TIMEOUT = "timeout"
_REASON_OTHER = "other"


def classify_download_error(exc: BaseException) -> str:
    """Bucket a media-download exception into a stable ``reason`` label.

    * ``auth_401`` — the Graph API rejected the access token (expired/revoked).
    * ``not_found`` — the media id didn't resolve. Graph reports an unknown or
      malformed id as a **400** GraphMethodException rather than a 404, so both
      land here; 404 is kept for the direct download URL going stale.
    * ``timeout`` — either hop exceeded ``TIMEOUT_SECONDS`` (connect or read).
    * ``other`` — everything else (5xx, missing download URL, bugs).
    """
    if isinstance(exc, requests.Timeout):
        return _REASON_TIMEOUT
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        if exc.response.status_code == 401:
            return _REASON_AUTH_401
        if exc.response.status_code in (400, 404):
            return _REASON_NOT_FOUND
    return _REASON_OTHER


def log_download_failure(exc: BaseException) -> None:
    """Emit one structured ``media_download`` failure line for #44 telemetry.

    Always WARNING: unlike a ``needs_review`` row, a failed download means the
    receipt's content is genuinely lost to the ledger (the row lands as an
    unreadable-image marker the owner must redo by hand).
    """
    log_event(
        _EVENT_MEDIA_DOWNLOAD,
        _OUTCOME_FAILURE,
        severity="WARNING",
        reason=classify_download_error(exc),
    )


def _access_token() -> str:
    """Return the WhatsApp access token, failing fast with a clear message."""
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN environment variable is not set")
    return token


def download_media(media_id: str) -> tuple[bytes, str]:
    """Download an inbound WhatsApp media object by its ID.

    Returns ``(bytes, mime_type)``. Raises on a missing token, a metadata
    response without a download URL, or any HTTP error — the caller treats a
    failed download the same as a failed parse and falls back to a raw row.
    """
    headers = {"Authorization": f"Bearer {_access_token()}"}

    metadata_response = requests.get(
        f"{GRAPH_API_BASE}/{media_id}", headers=headers, timeout=TIMEOUT_SECONDS
    )
    metadata_response.raise_for_status()
    metadata = metadata_response.json()

    url = metadata.get("url")
    if not url:
        raise RuntimeError(f"media {media_id} metadata has no download URL")
    mime_type = metadata.get("mime_type") or "application/octet-stream"

    binary_response = requests.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
    binary_response.raise_for_status()
    return binary_response.content, mime_type
