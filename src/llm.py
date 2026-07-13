"""Gemini adapter: turn a free-text cash-sale note into structured columns.

``coerce_note`` is pure domain logic (dict → ``ParsedNote``) and is unit-tested
directly. ``parse_note`` is the thin adapter over Gemini 2.5 Flash: it builds a
client, asks for JSON against the contract in ``docs/ARCHITECTURE.md``, and
coerces the result. Any failure (no key, API error, non-JSON) returns ``None``
so the caller can fall back to the raw-text row from Issue #1 — nothing is lost.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Any

from google import genai
from google.genai import types

_DEFAULT_MODEL = "gemini-2.5-flash"

# Strict system instruction. The model must return ONLY JSON matching the
# contract; ``response_mime_type`` below also forces JSON, but stating it keeps
# the model on-schema and honest about confidence.
_SYSTEM_INSTRUCTION = (
    "You extract a single catering ledger entry from a short note written by the "
    "business owner. Return ONLY a JSON object with these keys:\n"
    '  "date": ISO date YYYY-MM-DD (resolve relative dates like "today" against '
    "the provided current date),\n"
    '  "contract_name": the client/event name, or "" if none is stated,\n'
    '  "category": one of Ingredients, Staff Salary, Revenue, Equipment, '
    "Transport, Other,\n"
    '  "type": "Revenue" for money coming in (sales), "Expense" for money going out,\n'
    '  "amount": the numeric amount as a number (no currency symbol),\n'
    '  "notes": a short human-readable summary of the note,\n'
    '  "confidence": "high" if the fields are clearly stated, "low" if you had to '
    "guess.\n"
    "Never invent an amount or contract that is not implied by the note; leave it "
    'empty and set "confidence" to "low" instead.'
)


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


def coerce_note(data: dict[str, Any], raw_text: str, today: date) -> ParsedNote:
    """Coerce a (possibly messy) LLM JSON object into a ``ParsedNote``.

    Missing or wrong-typed fields fall back to safe defaults: today's date, the
    raw note text for Notes, and ``low`` confidence — so a partial parse still
    yields a usable, non-lossy row.
    """
    return ParsedNote(
        date=_as_text(data.get("date")) or today.isoformat(),
        contract_name=_as_text(data.get("contract_name")),
        category=_as_text(data.get("category")),
        type=_as_text(data.get("type")),
        amount=_as_amount(data.get("amount")),
        notes=_as_text(data.get("notes")) or raw_text,
        confidence="high" if _as_text(data.get("confidence")).lower() == "high" else "low",
    )


def _build_client() -> genai.Client:
    """Build a Gemini client from ``GEMINI_API_KEY`` (raises if unset)."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable is not set")
    return genai.Client(api_key=api_key)


def parse_note(text: str, today: date) -> ParsedNote | None:
    """Parse a free-text note into structured columns via Gemini 2.5 Flash.

    Returns a ``ParsedNote`` on success, or ``None`` on any failure (missing key,
    API error, non-JSON response) so the caller can fall back to a raw-text row.
    """
    prompt = f"Current date: {today.isoformat()}\nNote: {text}"
    try:
        client = _build_client()
        response = client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", _DEFAULT_MODEL),
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        data = json.loads(response.text or "")
    except Exception:
        # Deliberately broad: never let a parse hiccup drop the owner's message.
        return None
    if not isinstance(data, dict):
        return None
    return coerce_note(data, raw_text=text, today=today)
