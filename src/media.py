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
