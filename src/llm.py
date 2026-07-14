"""Gemini adapter: turn a free-text cash-sale note into structured columns.

``coerce_note`` is pure domain logic (dict → ``ParsedNote``) and is unit-tested
directly. ``parse_note`` is the thin adapter over Gemini Flash: it builds a
client, asks for JSON against the contract in ``docs/ARCHITECTURE.md``, and
coerces the result. Any failure (no key, API error, non-JSON) returns ``None``
so the caller can fall back to the raw-text row from Issue #1 — nothing is lost.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Any

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# Alias for the current Gemini Flash-Lite model (aliases avoid pins that get
# retired for new users). Flash-Lite over full Flash for free-tier reliability;
# rationale in docs/ARCHITECTURE.md. Override per-env with GEMINI_MODEL.
_DEFAULT_MODEL = "gemini-flash-lite-latest"

# Strict system instruction. The model must return ONLY JSON matching the
# contract; ``response_mime_type`` below also forces JSON, but stating it keeps
# the model on-schema and honest about confidence.
_SYSTEM_INSTRUCTION = (
    "You extract a single catering ledger entry from a short note written by the "
    "business owner. Return ONLY a JSON object with these keys:\n"
    '  "date": the date the TRANSACTION occurred, as ISO YYYY-MM-DD. Use a date '
    'stated in the note (e.g. "July 3rd", "2026-06-28"); resolve relative dates '
    '("today", "yesterday") against the provided current date; only if no date is '
    "stated at all, use the current date,\n"
    '  "contract_name": the CLIENT or account — who booked or pays, the ongoing '
    'relationship (e.g. "Diallo", "ONEP"); "" if none is stated,\n'
    '  "event": the specific occasion or function this entry belongs to (e.g. '
    '"Diallo wedding", "ONEP retreat", "Saturday market"); "" if none is stated,\n'
    '  "category": one of Ingredients, Staff Salary, Revenue, Equipment, '
    "Transport, Other,\n"
    '  "type": "Revenue" for money coming in (sales), "Expense" for money going out,\n'
    '  "amount": the numeric amount as a number (no currency symbol),\n'
    '  "notes": a short human-readable summary of the note,\n'
    '  "status": one of "Paid" (the money has already changed hands), "Owed to us" '
    "(we delivered or sold but have NOT been paid yet — credit given to the "
    'client), "Owed by us" (we bought on credit and have NOT paid the supplier '
    'yet). Default to "Paid" unless the note clearly implies credit or non-payment,\n'
    '  "confidence": "high" if the fields are clearly stated, "low" if you had to '
    "guess.\n"
    "Never invent an amount, contract, or event that is not implied by the note; "
    'leave it empty and set "confidence" to "low" instead.'
)

# Payment-settlement lifecycle. The model only sets the INITIAL state from the
# note; the owner flips a row to "Paid" in the Sheet when the money actually
# arrives. These three states let one flat ledger answer cash-on-hand,
# money-owed-to-us (receivables), and money-we-owe (payables).
_ALLOWED_STATUSES = ("Paid", "Owed to us", "Owed by us")


@dataclass(frozen=True)
class ParsedNote:
    """Structured ledger fields extracted from a note (the LLM contract)."""

    date: str
    contract_name: str
    category: str
    type: str
    amount: str
    notes: str
    confidence: str
    # Appended (with defaults) so existing positional construction stays valid;
    # coerce_note always sets both explicitly from the model's JSON.
    event: str = ""
    status: str = ""


def _as_text(value: Any) -> str:
    """Return a stripped string only for genuine strings; anything else → ''."""
    return value.strip() if isinstance(value, str) else ""


def _as_amount(value: Any) -> str:
    """Coerce an amount (number or numeric string) to a Sheet-ready string.

    Numbers are stringified, dropping the trailing ``.0`` on whole numbers so
    "200" lands in the Sheet rather than "200.0". A string is kept verbatim
    (the LLM may return "abc" or a currency-tagged value); anything else → ''.
    """
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    return _as_text(value)


def _as_status(value: Any) -> str:
    """Coerce a status to one of ``_ALLOWED_STATUSES`` (case-insensitive).

    Defaults to "Paid" — the common completed cash sale — when the model omits
    it or returns something off-list, so the Sheet's cash / receivable / payable
    math never sees a stray value. The owner corrects the rarer credit rows by
    flipping the Status cell in the Sheet.
    """
    text = _as_text(value)
    for allowed in _ALLOWED_STATUSES:
        if text.lower() == allowed.lower():
            return allowed
    return "Paid"


def coerce_note(data: dict[str, Any], raw_text: str, today: date) -> ParsedNote:
    """Coerce a (possibly messy) LLM JSON object into a ``ParsedNote``.

    Missing or wrong-typed fields fall back to safe defaults: today's date, the
    raw note text for Notes, and ``low`` confidence — so a partial parse still
    yields a usable, non-lossy row.
    """
    return ParsedNote(
        date=_as_text(data.get("date")) or today.isoformat(),
        contract_name=_as_text(data.get("contract_name")),
        event=_as_text(data.get("event")),
        category=_as_text(data.get("category")),
        type=_as_text(data.get("type")),
        amount=_as_amount(data.get("amount")),
        notes=_as_text(data.get("notes")) or raw_text,
        confidence="high" if _as_text(data.get("confidence")).lower() == "high" else "low",
        status=_as_status(data.get("status")),
    )


def _build_client() -> genai.Client:
    """Build a Gemini client from ``GEMINI_API_KEY`` (raises if unset)."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable is not set")
    return genai.Client(api_key=api_key)


def _generate_note(contents: Any, raw_text: str, today: date) -> ParsedNote | None:
    """Send ``contents`` to Gemini under the ledger contract and coerce the JSON.

    Shared by the text (``parse_note``) and image (``parse_image``) adapters —
    only the ``contents`` differ (a string vs. a prompt + inline image part).
    Returns ``None`` on any failure so the caller can fall back to a raw row;
    ``raw_text`` seeds ``coerce_note``'s Notes default when the model omits it.
    """
    try:
        client = _build_client()
        response = client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", _DEFAULT_MODEL),
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        # raw_decode parses the FIRST JSON value and ignores anything after it:
        # Gemini occasionally appends a stray "}" or trailing text even in JSON
        # mode, which json.loads would reject as "Extra data".
        data, _ = json.JSONDecoder().raw_decode((response.text or "").lstrip())
    except Exception:
        # Deliberately broad: never let a parse hiccup drop the owner's message.
        # Logged at WARNING (not ERROR) because falling back is expected on
        # transient throttling (free-tier 429) — exc_info keeps the cause
        # visible in Cloud Logging without alarming noise.
        logger.warning("Gemini parse failed; falling back to raw row", exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    return coerce_note(data, raw_text=raw_text, today=today)


def parse_note(text: str, today: date) -> ParsedNote | None:
    """Parse a free-text note into structured columns via Gemini Flash.

    Returns a ``ParsedNote`` on success, or ``None`` on any failure (missing key,
    API error, non-JSON response) so the caller can fall back to a raw-text row.
    """
    prompt = f"Current date: {today.isoformat()}\nNote: {text}"
    return _generate_note(prompt, raw_text=text, today=today)


def parse_image(
    image_bytes: bytes, mime_type: str, today: date, caption: str = ""
) -> ParsedNote | None:
    """Parse a receipt photo into structured columns via multimodal Gemini.

    Sends the image bytes inline alongside a text prompt, reusing the same JSON
    contract and system instruction as the text path. ``caption`` (the owner's
    optional photo caption) both steers the model and seeds the Notes fallback.
    Returns ``None`` on any failure so the caller can fall back to a raw row.
    """
    prompt = (
        f"Current date: {today.isoformat()}\n"
        "The attached image is a photo of a receipt or invoice for a business "
        "expense. Read it and extract the ledger entry."
    )
    if caption:
        prompt += f"\nThe owner added this caption: {caption}"
    contents = [
        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
        prompt,
    ]
    return _generate_note(contents, raw_text=caption, today=today)
