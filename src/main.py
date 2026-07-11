"""Cloud Function entry point for the catering-ledger 'Snap & Chat' webhook.

This is the walking skeleton (see Issues #1-#2). It currently:
  - answers the WhatsApp webhook verification handshake (GET)
  - accepts inbound message webhooks (POST) and ACKs them

Gemini parsing and Google Sheets persistence are added in later slices.
"""

from __future__ import annotations

import os

import functions_framework
from flask import Request


def verify_webhook(
    mode: str | None,
    token: str | None,
    challenge: str | None,
    expected_token: str | None,
) -> tuple[str, int]:
    """Validate Meta's webhook verification handshake.

    On success, Meta expects the ``challenge`` echoed back verbatim.
    Returns ``(body, status_code)``.
    """
    if mode == "subscribe" and token is not None and token == expected_token:
        return (challenge or "", 200)
    return ("forbidden", 403)


@functions_framework.http
def webhook(request: Request) -> tuple[str, int]:
    """HTTP entry point wired to the WhatsApp Cloud API."""
    if request.method == "GET":
        return verify_webhook(
            request.args.get("hub.mode"),
            request.args.get("hub.verify_token"),
            request.args.get("hub.challenge"),
            os.environ.get("WHATSAPP_VERIFY_TOKEN"),
        )

    if request.method == "POST":
        # TODO(#3, #4): parse text/image/voice via Gemini and append to the Sheet.
        return ("", 200)

    return ("method not allowed", 405)
