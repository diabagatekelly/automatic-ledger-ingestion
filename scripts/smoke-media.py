#!/usr/bin/env python
"""Live smoke test for Issue #44 — the WhatsApp media endpoint, for real.

``scripts/smoke-gemini-image.py`` reads bytes from disk, so it can NOT catch a
dead access token — that gap is exactly why the #5 expired-token incident needed
a human to notice bad rows. This script hits the real Graph media endpoint and
proves the two failure signatures we alert on classify correctly:

  1. auth_401   — a junk token (auth is checked before the media id lookup,
                  so any id works)
  2. not_found  — the real token + a bogus media id

Optionally, pass a real media id to also prove the happy path end-to-end:

    export WHATSAPP_ACCESS_TOKEN=...     # from .env
    python scripts/smoke-media.py [MEDIA_ID]

Makes real Graph API calls; nothing is written anywhere.
"""

from __future__ import annotations

import os
import sys

# Allow running from the repo root without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.media import classify_download_error, download_media  # noqa: E402

_BOGUS_MEDIA_ID = "1234567890"


def _expect_failure(label: str, media_id: str, expected_reason: str) -> bool:
    """Run a download that must fail, and check the classified reason."""
    print(f"\n{label}: download_media({media_id!r})")
    try:
        download_media(media_id)
    except Exception as exc:  # noqa: BLE001 — classifying arbitrary failures is the point
        reason = classify_download_error(exc)
        status = "OK" if reason == expected_reason else "MISMATCH"
        print(
            f"  raised {type(exc).__name__} -> reason={reason} "
            f"(expected {expected_reason}) {status}"
        )
        return reason == expected_reason
    print(f"  UNEXPECTED SUCCESS — expected a {expected_reason} failure")
    return False


def main() -> int:
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    if not token:
        print("ERROR: set WHATSAPP_ACCESS_TOKEN first (see .env.example).", file=sys.stderr)
        return 1

    ok = True

    # 1. auth_401 — junk token, any media id (auth is checked before lookup).
    os.environ["WHATSAPP_ACCESS_TOKEN"] = "junk-token-for-smoke-test"
    ok &= _expect_failure("auth_401", _BOGUS_MEDIA_ID, "auth_401")

    # 2. not_found — real token, bogus media id (Graph reports it as a 400).
    os.environ["WHATSAPP_ACCESS_TOKEN"] = token
    ok &= _expect_failure("not_found", _BOGUS_MEDIA_ID, "not_found")

    # 3. (optional) happy path — a real, recent media id from an inbound message.
    if len(sys.argv) > 1:
        media_id = sys.argv[1]
        print(f"\nhappy path: download_media({media_id!r})")
        data, mime_type = download_media(media_id)
        print(f"  OK — {len(data)} bytes, mime_type={mime_type}")

    print("\nAll media smoke checks passed." if ok else "\nSome checks FAILED (see above).")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
