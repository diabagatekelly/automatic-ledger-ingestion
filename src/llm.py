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
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from google import genai
from google.genai import errors, types

from src.telemetry import log_event

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

# Bounded retry for TRANSIENT Gemini failures (Issue #33). A 429 (per-minute rate
# limit) or 503 ("high demand") used to drop the owner's message straight to the
# raw-text fallback on a temporary blip; a short retry lets most clear on a resend.
#
# We own this policy rather than the SDK's: google-genai's tenacity retry is OFF
# by default (http_options.retry_options=None → stop_after_attempt(1)), so there
# is no double-waiting to guard against — and its defaults (max_delay 60s, six
# retryable codes) are far too loose for Meta's webhook ACK budget. Worst-case
# added wait here is ~0.5s + ~1.0s = ~1.5s (+jitter), well inside that budget, so
# the webhook still ACKs 200 in time (a non-200 makes Meta retry the delivery).
#
# Transient only: 429/503 surface as google.genai.errors.APIError (Client/Server
# Error) with an int .code; everything else (bad key, 400, non-JSON) falls through
# to the fallback immediately. We deliberately do NOT honour a `Retry-After`
# header — a 429 can advertise up to 60s, which we'd have to cap to _RETRY_MAX_DELAY
# anyway to protect the ACK budget, so bounded exponential backoff is simpler and
# strictly safer.
_RETRY_MAX_ATTEMPTS = 3  # initial call + up to 2 retries
_RETRY_INITIAL_DELAY = 0.5  # seconds before the first retry
_RETRY_EXP_BASE = 2.0  # delay multiplier after each attempt
_RETRY_MAX_DELAY = 4.0  # per-retry wait cap, keeps the total bounded
_RETRY_JITTER = 0.25  # add [0, delay * _RETRY_JITTER] to avoid synchronised retries

# Parse-outcome telemetry (Issue #34). Every parse emits one structured JSON log
# line (see _log_parse_outcome) tagged with a stable ``reason`` bucket, counted by
# a Cloud Monitoring log-based metric — see docs/OBSERVABILITY.md for the metric +
# Logs Explorer query. ``_classify_error`` is the SINGLE source of transient-vs-
# non-transient truth, shared with #33's retry decision (transient == one of
# ``_TRANSIENT_REASONS``) so the retry policy and the telemetry never drift.
_REASON_TRANSIENT_429 = "transient_429"
_REASON_TRANSIENT_503 = "transient_503"
_REASON_NO_API_KEY = "no_api_key"
_REASON_INVALID_API_KEY = "invalid_api_key"
_REASON_BAD_JSON = "bad_json"
_REASON_EMPTY_RESPONSE = "empty_response"
_REASON_BAD_REQUEST_400 = "bad_request_400"
_REASON_OTHER = "other"
_TRANSIENT_REASONS = (_REASON_TRANSIENT_429, _REASON_TRANSIENT_503)

# ``outcome`` is three-state (#9). ``success`` means a CLEAN row landed —
# ``needs_review`` means one landed but wants the owner's eye, and ``fallback``
# means the parse failed and the row is raw text. Two states conflated the first
# two: a photo of a mat and a real receipt both logged plain "success", so the
# dashboards stayed green while the ledger filled with junk.
_OUTCOME_SUCCESS = "success"
_OUTCOME_NEEDS_REVIEW = "needs_review"
_OUTCOME_FALLBACK = "fallback"

# Why a row needs review. See review_reason for why the precedence matters.
_REVIEW_NO_AMOUNT = "no_amount"
_REVIEW_LOW_CONFIDENCE = "low_confidence"


class MissingAPIKeyError(RuntimeError):
    """Raised when ``GEMINI_API_KEY`` is unset — a config error, typed so the
    telemetry classifier can bucket it distinctly (``no_api_key``) rather than
    string-matching a generic ``RuntimeError``."""


class EmptyResponseError(RuntimeError):
    """Raised when Gemini returns no text at all (``response.text`` is None or
    blank) — a safety block or a no-candidates response.

    Typed so the classifier can bucket it as ``empty_response``: it used to reach
    ``raw_decode("")`` and surface as ``bad_json``, which reads as "the model sent
    garbage" when in fact the model sent nothing. Different cause, different fix."""


def _classify_error(exc: BaseException) -> str:
    """Bucket a parse exception into a stable ``reason`` (telemetry + retry).

    The transient buckets (``transient_429``/``transient_503``) are exactly what
    #33 retries; every other bucket is non-transient and falls back immediately.
    """
    if isinstance(exc, errors.APIError):
        if exc.code == 429:
            return _REASON_TRANSIENT_429
        if exc.code == 503:
            return _REASON_TRANSIENT_503
        # How the API rejects unusable input — corrupt image bytes, an
        # unsupported mime type, an oversized payload. Split out because that is
        # an actionable "the thing we sent was wrong", unlike a generic failure.
        if exc.code == 400:
            return _REASON_BAD_REQUEST_400
        # A key that is PRESENT but rejected (revoked, mistyped, wrong project).
        # Distinct from no_api_key — same blind-spot family as #44's auth_401:
        # both are config errors, but "mount the secret" and "fix the key" are
        # different runbooks, and `other` hides them both.
        if exc.code in (401, 403):
            return _REASON_INVALID_API_KEY
        return _REASON_OTHER
    if isinstance(exc, MissingAPIKeyError):
        return _REASON_NO_API_KEY
    if isinstance(exc, EmptyResponseError):
        return _REASON_EMPTY_RESPONSE
    if isinstance(exc, json.JSONDecodeError):
        return _REASON_BAD_JSON
    return _REASON_OTHER


def _log_parse_outcome(
    outcome: str, *, reason: str | None = None, confidence: str | None = None
) -> None:
    """Emit one structured JSON log line per parse outcome for #34 telemetry.

    Printed as a single-line JSON object to stdout so Cloud Run's logging agent
    parses it into ``jsonPayload`` with queryable fields (``event``/``outcome``/
    ``reason``/``confidence``) — no logging library or external service needed. A
    Cloud Monitoring log-based metric counts these; see docs/OBSERVABILITY.md.

    ``outcome`` is three-state (#9): ``success`` (clean row), ``needs_review``
    (row landed but wants the owner's eye) or ``fallback`` (parse failed, raw-text
    row). ``reason`` explains the latter two — an error bucket for ``fallback``, a
    review trigger for ``needs_review``. ``confidence`` rides on both parsed
    outcomes and is omitted on ``fallback``, where no note was produced and
    guessing one would be a lie.

    Only ``fallback`` is a WARNING. A ``needs_review`` row LANDED — nothing broke,
    and this trigger is expected to fire often, so crying wolf about it would
    train the reader to ignore the level.
    """
    log_event(
        "gemini_parse",
        outcome,
        severity="WARNING" if outcome == _OUTCOME_FALLBACK else "INFO",
        reason=reason,
        confidence=confidence,
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


def has_usable_amount(amount: str) -> bool:
    """Whether an Amount cell holds a real, actionable figure.

    Blank, non-numeric ("abc", "200 CFA") and **zero** all mean "no amount". Zero
    is included because the model does NOT reliably leave the field empty when it
    finds nothing: a live run on the contentless text "Recu paiement" returned
    ``amount: 0`` despite the system instruction saying never to invent one, which
    would otherwise sail through as a confident "✅ Logged: Revenue 0". A
    zero-value catering entry carries no financial meaning, so treating it as
    absent costs nothing and stops a fabricated number reaching the ledger.
    Negatives stay valid — a refund is a real figure.

    Shared by ``review_reason`` and ``whatsapp.build_confirmation`` so the Sheet,
    the reply and the logs can't disagree about whether a row is usable.
    """
    try:
        return float(amount.strip()) != 0
    except ValueError:
        return False


def review_reason(note: ParsedNote) -> str | None:
    """Why a parsed row needs the owner's eye, or ``None`` if it's clean (#9).

    The single source of the review decision, deliberately here rather than in
    the Sheets adapter: ``_generate_note`` logs it, ``sheets.build_row_from_note``
    flags on it, and one function means the telemetry and the ledger can never
    disagree about what a row is.

    Precedence is load-bearing. A row can trip both triggers — the live
    "Recu paiement" case returned ``amount: 0`` *and* ``confidence: low`` — but a
    log-based metric label needs a single value, so we report the more actionable
    one. ``no_amount`` is a **fact** about the row; ``low_confidence`` is only the
    model's **opinion** of itself, and a poorly calibrated one: a 2026-07-15 smoke
    run scored a complete, correct row ``low`` while a sparser row scored ``high``.

    That ordering is what makes the buckets worth counting: ``low_confidence``
    means "flagged *only* because the model doubted, row otherwise fine" — i.e.
    the candidate noise — while ``no_amount`` means the row is genuinely unusable.
    The split between them answers whether the low-confidence trigger earns its
    place, without having to ask the owner to notice.
    """
    if not has_usable_amount(note.amount):
        return _REVIEW_NO_AMOUNT
    if note.confidence == "low":
        return _REVIEW_LOW_CONFIDENCE
    return None


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
        raise MissingAPIKeyError("GEMINI_API_KEY environment variable is not set")
    return genai.Client(api_key=api_key)


def _generate_with_retry(
    client: genai.Client,
    *,
    model: str,
    contents: Any,
    config: types.GenerateContentConfig,
    sleep: Callable[[float], None] | None = None,
    uniform: Callable[[float, float], float] | None = None,
) -> Any:
    """Call Gemini, retrying only transient (429/503) errors with bounded backoff.

    Non-transient errors (bad key, 400, malformed request) and the final failed
    attempt re-raise immediately, so the caller falls back to a raw-text row
    without wasted waiting. Total added latency is capped by the retry constants
    to stay inside Meta's webhook ACK window.

    ``sleep`` and ``uniform`` default to ``time.sleep`` / ``random.uniform`` and
    exist to be injected in tests, so the backoff schedule can be observed and
    made deterministic without patching global modules.
    """
    _sleep = time.sleep if sleep is None else sleep
    _uniform = random.uniform if uniform is None else uniform
    delay = _RETRY_INITIAL_DELAY
    attempt = 1
    # ``while True`` (not ``for``) so ``return``/``raise`` are the only exits and
    # the control flow is structurally exhaustive — no unreachable trailing raise.
    while True:
        try:
            return client.models.generate_content(model=model, contents=contents, config=config)
        except errors.APIError as exc:
            # Same classifier the telemetry uses (#34), so "what we retry" and
            # "what we count as transient" can never diverge.
            if _classify_error(exc) not in _TRANSIENT_REASONS or attempt == _RETRY_MAX_ATTEMPTS:
                raise
            wait = delay + _uniform(0.0, delay * _RETRY_JITTER)
            logger.info(
                "Gemini transient %s on attempt %d/%d; retrying in %.2fs",
                exc.code,
                attempt,
                _RETRY_MAX_ATTEMPTS,
                wait,
            )
            _sleep(wait)
            delay = min(delay * _RETRY_EXP_BASE, _RETRY_MAX_DELAY)
            attempt += 1


def _generate_note(contents: Any, raw_text: str, today: date) -> ParsedNote | None:
    """Send ``contents`` to Gemini under the ledger contract and coerce the JSON.

    Shared by the text (``parse_note``) and image (``parse_image``) adapters —
    only the ``contents`` differ (a string vs. a prompt + inline image part).
    Returns ``None`` on any failure so the caller can fall back to a raw row;
    ``raw_text`` seeds ``coerce_note``'s Notes default when the model omits it.
    """
    try:
        client = _build_client()
        response = _generate_with_retry(
            client,
            model=os.environ.get("GEMINI_MODEL", _DEFAULT_MODEL),
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        text = (response.text or "").lstrip()
        if not text:
            # Nothing came back — a safety block or a no-candidates response.
            # Guarded explicitly because raw_decode("") raises JSONDecodeError,
            # which would bucket a silent refusal as bad_json (#9).
            raise EmptyResponseError("Gemini returned an empty response body")
        # raw_decode parses the FIRST JSON value and ignores anything after it:
        # Gemini occasionally appends a stray "}" or trailing text even in JSON
        # mode, which json.loads would reject as "Extra data".
        data, _ = json.JSONDecoder().raw_decode(text)
    except Exception as exc:
        # Deliberately broad: never let a parse hiccup drop the owner's message.
        # One structured outcome line (queryable, drives the log-based metric #34)
        # plus the human WARNING with exc_info — falling back is expected on
        # transient throttling (free-tier 429), so WARNING (not ERROR) keeps the
        # cause visible in Cloud Logging without alarming noise.
        _log_parse_outcome(_OUTCOME_FALLBACK, reason=_classify_error(exc))
        logger.warning("Gemini parse failed; falling back to raw row", exc_info=True)
        return None
    if not isinstance(data, dict):
        # Valid JSON but not an object (e.g. a list) is unusable — same bucket as
        # a decode failure from the owner's point of view. Mirror the decode
        # path's human-readable WARNING (there's no exception here) so this stays
        # diagnosable if the model starts returning non-objects; the repr is
        # truncated so a large value can't flood the logs.
        _log_parse_outcome(_OUTCOME_FALLBACK, reason=_REASON_BAD_JSON)
        logger.warning(
            "Gemini returned non-object JSON (type=%s, value=%s); falling back to raw row",
            type(data).__name__,
            repr(data)[:200],
        )
        return None
    # Coerce BEFORE logging so the reported confidence is the coerced value that
    # actually lands in the row — not the model's raw claim, which coerce_note
    # normalises (anything but "high" is low). The telemetry and the Sheet then
    # can't disagree about how trustworthy a row is.
    note = coerce_note(data, raw_text=raw_text, today=today)
    # Same call sheets.build_row_from_note makes to decide the NEEDS_REVIEW flag,
    # so a row logged needs_review is exactly a row flagged in the Sheet.
    reason = review_reason(note)
    _log_parse_outcome(
        _OUTCOME_NEEDS_REVIEW if reason else _OUTCOME_SUCCESS,
        reason=reason,
        confidence=note.confidence,
    )
    return note


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
