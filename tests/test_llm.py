import json
import logging
from datetime import date

import pytest
from google.genai import errors, types

from src.llm import (
    _RETRY_INITIAL_DELAY,
    _RETRY_MAX_ATTEMPTS,
    EmptyResponseError,
    MissingAPIKeyError,
    ParsedNote,
    _build_client,
    _classify_error,
    _generate_with_retry,
    coerce_note,
    parse_image,
    parse_note,
    review_reason,
)

TODAY = date(2026, 7, 13)


def _parsed(**overrides: str) -> ParsedNote:
    """A clean, fully-populated ParsedNote; override just the field under test."""
    base = {
        "date": "2026-07-13",
        "contract_name": "Diallo",
        "event": "Diallo wedding",
        "category": "Ingredients",
        "type": "Expense",
        "amount": "200",
        "notes": "Meat for the wedding",
        "confidence": "high",
        "status": "Paid",
    }
    return ParsedNote(**{**base, **overrides})  # type: ignore[arg-type]


class FakeResponse:
    def __init__(self, text: str | None) -> None:
        self.text = text


class FakeClient:
    """Stand-in for genai.Client whose generate_content returns a canned body."""

    def __init__(self, text: str | None = None, raises: Exception | None = None) -> None:
        self._text = text
        self._raises = raises
        self.models = self
        self.last_kwargs: dict[str, object] = {}

    def generate_content(self, **kwargs: object) -> FakeResponse:
        self.last_kwargs = kwargs
        if self._raises is not None:
            raise self._raises
        assert self._text is not None
        return FakeResponse(self._text)


class NoTextClient:
    """generate_content returns a response whose .text is None.

    What the SDK gives back when a response carries no candidates at all — e.g.
    a safety block. Distinct from a malformed body: nothing was returned to parse.
    """

    def __init__(self) -> None:
        self.models = self

    def generate_content(self, **kwargs: object) -> FakeResponse:
        return FakeResponse(None)


class FlakyClient:
    """generate_content raises each queued exception in turn, then returns text."""

    def __init__(self, failures: list[Exception], text: str) -> None:
        self._failures = list(failures)
        self._text = text
        self.models = self
        self.calls = 0

    def generate_content(self, **kwargs: object) -> FakeResponse:
        self.calls += 1
        if self._failures:
            raise self._failures.pop(0)
        return FakeResponse(self._text)


def _api_error(code: int) -> errors.APIError:
    """Build an errors.APIError with a given HTTP status code (503, 429, 400…)."""
    return errors.APIError(
        code, {"error": {"code": code, "status": "TRANSIENT", "message": "test"}}
    )


# A minimal but valid Gemini JSON body — coerce_note fills the rest with defaults.
_VALID_JSON = '{"amount": 200, "type": "Revenue", "category": "Revenue", "confidence": "high"}'


# --- coerce_note (pure) ---


def test_coerce_note_maps_a_full_valid_payload() -> None:
    note = coerce_note(
        {
            "date": "2026-07-13",
            "contract_name": "Diallo",
            "event": "Diallo wedding",
            "category": "Revenue",
            "type": "Revenue",
            "amount": 200,
            "notes": "Cash sale",
            "confidence": "high",
            "status": "Owed to us",
        },
        raw_text="Cash sale, $200, Diallo wedding",
        today=TODAY,
    )
    assert note == ParsedNote(
        date="2026-07-13",
        contract_name="Diallo",
        event="Diallo wedding",
        category="Revenue",
        type="Revenue",
        amount="200",
        notes="Cash sale",
        confidence="high",
        status="Owed to us",
    )


def test_coerce_note_formats_a_decimal_amount() -> None:
    note = coerce_note({"amount": 49.5}, raw_text="x", today=TODAY)
    assert note.amount == "49.5"


def test_coerce_note_defaults_missing_fields() -> None:
    note = coerce_note({}, raw_text="Cash sale, $200", today=TODAY)
    assert note.date == "2026-07-13"  # falls back to today
    assert note.contract_name == ""
    assert note.event == ""
    assert note.category == ""
    assert note.type == ""
    assert note.amount == ""
    assert note.notes == "Cash sale, $200"  # falls back to the raw text
    assert note.confidence == "low"  # unknown → treated as low
    assert note.status == "Paid"  # missing status defaults to a completed cash sale


def test_coerce_note_normalizes_and_defaults_status() -> None:
    # Case-insensitive AND whitespace-insensitive match to the allowed set
    # (_as_text strips); anything off-list → "Paid", so the Sheet's
    # cash/receivable/payable math never sees a stray value.
    assert coerce_note({"status": "owed to us"}, raw_text="x", today=TODAY).status == "Owed to us"
    assert coerce_note({"status": "OWED BY US"}, raw_text="x", today=TODAY).status == "Owed by us"
    padded = coerce_note({"status": "  Owed to us  "}, raw_text="x", today=TODAY)
    assert padded.status == "Owed to us"
    assert coerce_note({"status": "later maybe"}, raw_text="x", today=TODAY).status == "Paid"


def test_coerce_note_tolerates_wrong_types() -> None:
    note = coerce_note(
        {"date": None, "contract_name": 5, "amount": True, "confidence": "HIGH"},
        raw_text="raw",
        today=TODAY,
    )
    assert note.date == "2026-07-13"
    assert note.contract_name == ""  # non-str dropped
    assert note.amount == ""  # bool is not a real amount → dropped
    assert note.confidence == "high"  # case-insensitive


def test_coerce_note_keeps_a_string_amount_verbatim() -> None:
    assert coerce_note({"amount": "abc"}, raw_text="x", today=TODAY).amount == "abc"


# --- parse_note (Gemini adapter, mocked client) ---


def test_parse_note_returns_structured_note_from_gemini_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps(
        {
            "date": "2026-07-13",
            "contract_name": "Wedding Cake",
            "category": "Revenue",
            "type": "Revenue",
            "amount": 200,
            "notes": "Cash sale",
            "confidence": "high",
        }
    )
    client = FakeClient(text=body)
    monkeypatch.setattr("src.llm._build_client", lambda: client)

    note = parse_note("Cash sale, $200, Wedding Cake", TODAY)

    assert note is not None
    assert note.contract_name == "Wedding Cake"
    assert note.amount == "200"
    # Both the note text and the current date go into the prompt — the latter is
    # what the system instruction resolves relative dates ("today") against.
    contents = str(client.last_kwargs.get("contents"))
    assert "Cash sale, $200, Wedding Cake" in contents
    assert TODAY.isoformat() in contents


def test_parse_note_fills_defaults_for_a_sparse_gemini_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A valid but mostly-empty object must still yield a usable row via
    # coerce_note's defaults (today's date, raw text in notes, low confidence).
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(text='{"amount": 200}'))

    note = parse_note("Cash sale", TODAY)

    assert note is not None
    assert note.amount == "200"
    assert note.date == TODAY.isoformat()
    assert note.notes == "Cash sale"  # falls back to the raw note
    assert note.confidence == "low"


def test_parse_note_returns_none_when_model_returns_non_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(text="not json at all"))
    assert parse_note("Cash sale", TODAY) is None


def test_parse_note_returns_none_when_model_call_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.llm._build_client", lambda: FakeClient(raises=RuntimeError("api down"))
    )
    assert parse_note("Cash sale", TODAY) is None


def test_parse_note_returns_none_when_client_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> object:
        raise RuntimeError("GEMINI_API_KEY not set")

    monkeypatch.setattr("src.llm._build_client", boom)
    assert parse_note("Cash sale", TODAY) is None


def test_parse_note_tolerates_trailing_data_after_the_json_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Gemini (even in JSON mode) occasionally appends a stray "}" or extra text
    # after the object. Parse the first JSON value rather than dropping the note.
    body = '{"contract_name": "Wedding Cake", "amount": 200, "confidence": "high"}\n}'
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(text=body))

    note = parse_note("Cash sale, $200, Wedding Cake", TODAY)

    assert note is not None
    assert note.contract_name == "Wedding Cake"
    assert note.amount == "200"


def test_parse_note_returns_none_when_model_returns_non_object_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Valid JSON that isn't an object (a list) must not become a row.
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(text="[1, 2, 3]"))
    assert parse_note("Cash sale", TODAY) is None


# --- bounded retry on transient (429/503) errors (Issue #33) ---
#
# The direct _generate_with_retry tests inject ``sleep``/``uniform`` so the
# backoff schedule is observable and deterministic without patching global
# modules; the end-to-end parse_note/parse_image tests prove the retry actually
# yields a PARSED row (not the raw-text fallback).


def _no_jitter(_low: float, _high: float) -> float:
    """Deterministic stand-in for random.uniform: always the low bound (no jitter)."""
    return _low


def _call_with_retry(
    client: object, slept: list[float]
) -> object:  # returns the Gemini response or raises
    return _generate_with_retry(
        client,  # type: ignore[arg-type]
        model="m",
        contents="x",
        config=types.GenerateContentConfig(),
        sleep=slept.append,
        uniform=_no_jitter,
    )


def test_generate_with_retry_uses_bounded_exponential_backoff() -> None:
    # Two transient failures then success → the response, with a deterministic
    # 0.5s, 1.0s (initial × exp_base) backoff and no jitter.
    slept: list[float] = []
    client = FlakyClient([_api_error(503), _api_error(429)], _VALID_JSON)

    response = _call_with_retry(client, slept)

    assert response.text == _VALID_JSON  # type: ignore[attr-defined]
    assert client.calls == 3
    assert slept == [_RETRY_INITIAL_DELAY, _RETRY_INITIAL_DELAY * 2]


def test_generate_with_retry_does_not_retry_non_transient_error() -> None:
    # A non-transient error (400) re-raises immediately — no wasted retries/sleeps.
    slept: list[float] = []
    client = FlakyClient([_api_error(400)], _VALID_JSON)

    with pytest.raises(errors.APIError) as excinfo:
        _call_with_retry(client, slept)

    assert excinfo.value.code == 400
    assert client.calls == 1
    assert slept == []


def test_generate_with_retry_reraises_after_exhausting_attempts() -> None:
    # Persistent 503 → all attempts used, then the last error propagates (so
    # _generate_note falls back to a raw-text row).
    slept: list[float] = []
    client = FlakyClient([_api_error(503)] * _RETRY_MAX_ATTEMPTS, _VALID_JSON)

    with pytest.raises(errors.APIError) as excinfo:
        _call_with_retry(client, slept)

    assert excinfo.value.code == 503
    assert client.calls == _RETRY_MAX_ATTEMPTS
    assert len(slept) == _RETRY_MAX_ATTEMPTS - 1  # one wait between each attempt


@pytest.mark.parametrize("code", [503, 429])
def test_parse_note_retries_transient_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    # End-to-end: a transient 503/429 that clears on a later attempt yields a
    # PARSED row, not the raw-text fallback.
    monkeypatch.setattr("src.llm.time.sleep", lambda _s: None)
    client = FlakyClient([_api_error(code), _api_error(code)], _VALID_JSON)
    monkeypatch.setattr("src.llm._build_client", lambda: client)

    note = parse_note("Cash sale, 200", TODAY)

    assert note is not None
    assert note.amount == "200"
    assert client.calls == 3  # 2 transient failures, then success


def test_parse_note_falls_back_after_exhausting_transient_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End-to-end: persistent 503 → attempts exhausted → the raw-text fallback.
    monkeypatch.setattr("src.llm.time.sleep", lambda _s: None)
    client = FlakyClient([_api_error(503)] * _RETRY_MAX_ATTEMPTS, _VALID_JSON)
    monkeypatch.setattr("src.llm._build_client", lambda: client)

    assert parse_note("busy", TODAY) is None
    assert client.calls == _RETRY_MAX_ATTEMPTS


def test_parse_image_retries_transient_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The retry lives in the shared _generate_note, so the image path benefits too.
    monkeypatch.setattr("src.llm.time.sleep", lambda _s: None)
    client = FlakyClient([_api_error(503)], _VALID_JSON)
    monkeypatch.setattr("src.llm._build_client", lambda: client)

    note = parse_image(b"\x89PNG", "image/png", TODAY, caption="receipt")

    assert note is not None
    assert client.calls == 2


# --- parse-outcome telemetry (Issue #34) ---
#
# Every parse emits one structured JSON log line (printed to stdout so Cloud
# Run parses it into jsonPayload). The classifier that buckets the failure is
# shared with #33's transient-vs-non-transient retry decision.


def _outcome_entries(capsys: pytest.CaptureFixture[str]) -> list[dict[str, str]]:
    """Extract the structured gemini_parse JSON log lines from captured stdout."""
    entries: list[dict[str, str]] = []
    for line in capsys.readouterr().out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("event") == "gemini_parse":
            entries.append(obj)
    return entries


def test_classify_error_buckets_every_failure_mode() -> None:
    assert _classify_error(_api_error(429)) == "transient_429"
    assert _classify_error(_api_error(503)) == "transient_503"
    assert _classify_error(_api_error(400)) == "bad_request_400"
    # An INVALID key (present but rejected) is distinct from a MISSING one:
    # both are config errors, but they have different fixes (#44).
    assert _classify_error(_api_error(401)) == "invalid_api_key"
    assert _classify_error(_api_error(403)) == "invalid_api_key"
    assert _classify_error(_api_error(418)) == "other"


def test_classify_error_spots_an_invalid_key_inside_a_400() -> None:
    """The REAL Gemini API rejects a bad key as 400 API_KEY_INVALID, not 401.

    Verified live 2026-07-16: a junk key raises APIError(code=400,
    status=INVALID_ARGUMENT) with an ErrorInfo detail reason=API_KEY_INVALID.
    Without this check the invalid-key case hides inside bad_request_400 —
    which reads as "the thing we sent was wrong", the wrong runbook entirely.
    """
    invalid_key = errors.APIError(
        400,
        {
            "error": {
                "code": 400,
                "message": "API key not valid. Please pass a valid API key.",
                "status": "INVALID_ARGUMENT",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "API_KEY_INVALID",
                        "domain": "googleapis.com",
                    }
                ],
            }
        },
    )
    assert _classify_error(invalid_key) == "invalid_api_key"
    # A 400 WITHOUT the API_KEY_INVALID signature stays bad_request_400.
    assert _classify_error(_api_error(400)) == "bad_request_400"
    assert _classify_error(MissingAPIKeyError("no key")) == "no_api_key"
    assert _classify_error(EmptyResponseError("empty")) == "empty_response"
    assert _classify_error(json.JSONDecodeError("bad", "doc", 0)) == "bad_json"
    assert _classify_error(ValueError("boom")) == "other"


def test_parse_note_logs_success_outcome(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(text=_VALID_JSON))

    assert parse_note("Cash sale, 200", TODAY) is not None

    entries = _outcome_entries(capsys)
    assert len(entries) == 1
    entry = entries[0]
    # Assert the full structured-log contract, not just the outcome.
    assert entry["event"] == "gemini_parse"
    assert entry["severity"] == "INFO"
    assert entry["outcome"] == "success"
    assert "reason" not in entry
    assert entry["message"].startswith("gemini_parse outcome=success")


@pytest.mark.parametrize("code, reason", [(429, "transient_429"), (503, "transient_503")])
def test_parse_note_logs_transient_fallback_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    code: int,
    reason: str,
) -> None:
    monkeypatch.setattr("src.llm.time.sleep", lambda _s: None)
    client = FlakyClient([_api_error(code)] * _RETRY_MAX_ATTEMPTS, _VALID_JSON)
    monkeypatch.setattr("src.llm._build_client", lambda: client)

    assert parse_note("busy", TODAY) is None

    entries = _outcome_entries(capsys)
    assert len(entries) == 1
    entry = entries[0]
    # Full contract on a fallback: event, WARNING severity, reason, message prefix.
    assert entry["event"] == "gemini_parse"
    assert entry["severity"] == "WARNING"
    assert entry["outcome"] == "fallback"
    assert entry["reason"] == reason
    assert entry["message"].startswith("gemini_parse outcome=fallback")


def test_parse_note_logs_no_api_key_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Real _build_client (not patched) so the missing-key error is raised + classified.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    assert parse_note("hi", TODAY) is None

    assert _outcome_entries(capsys)[0]["reason"] == "no_api_key"


def test_parse_note_logs_bad_json_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(text="not json"))

    assert parse_note("hi", TODAY) is None

    assert _outcome_entries(capsys)[0]["reason"] == "bad_json"


def test_parse_note_logs_bad_json_for_non_object_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Valid JSON but not an object (a list) is unusable — same bucket as a decode error.
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(text="[1, 2, 3]"))

    with caplog.at_level(logging.WARNING, logger="src.llm"):
        assert parse_note("hi", TODAY) is None

    assert _outcome_entries(capsys)[0]["reason"] == "bad_json"
    # Mirrors the decode path with a human-readable WARNING naming the bad type.
    assert "non-object" in caplog.text.lower()
    assert "list" in caplog.text.lower()


def test_parse_image_logs_outcome(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The image path shares _generate_note, so it emits telemetry too.
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(text=_VALID_JSON))

    assert parse_image(b"x", "image/png", TODAY, caption="") is not None

    assert _outcome_entries(capsys)[0]["outcome"] == "success"


# --- the three-state outcome (Issue #9) ---
#
# success      — a clean row, lands unflagged
# needs_review — parsed fine, but missing info or bogus data; row lands FLAGGED
# fallback     — the parse itself failed; raw-text row
#
# The review decision is computed here, upstream of the Sheet, so the telemetry
# and the NEEDS_REVIEW flag can never disagree about what a row is. Before this,
# a junk row and a good row both logged plain "success" and were indistinguishable.


def test_review_reason_is_none_for_a_clean_row() -> None:
    assert review_reason(_parsed(amount="200", confidence="high")) is None


@pytest.mark.parametrize("amount", ["", "   ", "0", "0.0", "abc"])
def test_review_reason_is_no_amount_when_the_figure_is_unusable(amount: str) -> None:
    assert review_reason(_parsed(amount=amount, confidence="high")) == "no_amount"


def test_review_reason_is_low_confidence_when_only_the_model_doubts() -> None:
    # The wedding-cake case: a complete, correct row the model scored low. This
    # is the bucket that answers "is the low-confidence trigger just noise?".
    assert review_reason(_parsed(amount="200", confidence="low")) == "low_confidence"


def test_review_reason_prefers_no_amount_when_both_triggers_fire() -> None:
    # "Recu paiement" trips both: amount=0 AND confidence=low. Report the
    # unambiguous, actionable one — a metric label needs a single value, and
    # "no amount" is the fact; "low confidence" is only the model's opinion.
    assert review_reason(_parsed(amount="0", confidence="low")) == "no_amount"


@pytest.mark.parametrize("stated, expected", [("high", "high"), ("low", "low")])
def test_parse_note_logs_confidence_on_every_parsed_row(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stated: str,
    expected: str,
) -> None:
    body = json.dumps({"amount": 200, "type": "Revenue", "confidence": stated})
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(text=body))

    assert parse_note("Cash sale, 200", TODAY) is not None

    assert _outcome_entries(capsys)[0]["confidence"] == expected


def test_parse_note_logs_success_only_for_an_unflagged_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    body = json.dumps({"amount": 200, "type": "Revenue", "confidence": "high"})
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(text=body))

    assert parse_note("Cash sale, 200", TODAY) is not None

    entry = _outcome_entries(capsys)[0]
    assert entry["outcome"] == "success"
    assert "reason" not in entry  # nothing to review


def test_parse_note_logs_needs_review_for_low_confidence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    body = json.dumps({"amount": 200, "type": "Revenue", "confidence": "low"})
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(text=body))

    assert parse_note("Cash sale, 200", TODAY) is not None

    entry = _outcome_entries(capsys)[0]
    assert entry["outcome"] == "needs_review"
    assert entry["reason"] == "low_confidence"
    assert entry["confidence"] == "low"
    # A flagged row still LANDED — it is not an error and must not cry wolf in
    # the logs, especially since this trigger is expected to fire often.
    assert entry["severity"] == "INFO"


def test_parse_note_logs_needs_review_for_a_fabricated_zero_amount(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The live "Recu paiement" case: the model invents amount=0 rather than
    # leaving it blank, despite the system instruction telling it not to.
    body = json.dumps({"amount": 0, "type": "Revenue", "confidence": "low"})
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(text=body))

    assert parse_note("Recu paiement", TODAY) is not None

    entry = _outcome_entries(capsys)[0]
    assert entry["outcome"] == "needs_review"
    assert entry["reason"] == "no_amount"


def test_needs_review_is_distinguishable_from_fallback_in_the_logs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The whole point: a junk row that PARSED is not the same event as a parse
    # that FAILED, and the ledger cares about the difference.
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(text="not json"))
    assert parse_note("hi", TODAY) is None

    assert _outcome_entries(capsys)[0]["outcome"] == "fallback"


def test_parse_note_logs_the_coerced_confidence_not_the_raw_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # coerce_note treats anything that isn't "high" as low; the telemetry must
    # report what actually landed in the row, not what the model claimed.
    body = json.dumps({"amount": 200, "confidence": "medium"})
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(text=body))

    note = parse_note("hi", TODAY)

    assert note is not None
    assert _outcome_entries(capsys)[0]["confidence"] == note.confidence == "low"


def test_junk_image_logs_needs_review_not_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The photo-of-a-mat case: Gemini obeys the schema and returns empty fields.
    # Still NOT a fallback — the parse worked. But it must not read as a clean
    # success either, which is exactly what it used to do.
    body = json.dumps({"amount": "", "contract_name": "", "confidence": "low"})
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(text=body))

    assert parse_image(b"x", "image/png", TODAY, caption="") is not None

    entry = _outcome_entries(capsys)[0]
    assert entry["outcome"] == "needs_review"
    assert entry["reason"] == "no_amount"
    assert entry["severity"] == "INFO"
    assert entry["confidence"] == "low"


def test_fallback_logs_no_confidence_field(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A fallback never produced a note, so there is no confidence to report —
    # the field must be absent rather than guessed at.
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(text="not json"))

    assert parse_note("hi", TODAY) is None

    entry = _outcome_entries(capsys)[0]
    assert entry["outcome"] == "fallback"
    assert "confidence" not in entry


# --- reason buckets that used to be mislabelled (Issue #9) ---


def test_parse_note_logs_empty_response_not_bad_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A safety-blocked / no-candidates response is empty, not malformed. It used
    # to reach raw_decode("") and surface as bad_json, hiding a distinct failure.
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(text="   "))

    assert parse_note("hi", TODAY) is None

    assert _outcome_entries(capsys)[0]["reason"] == "empty_response"


def test_parse_note_logs_empty_response_when_text_is_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # response.text is None when the SDK returns no candidates at all.
    monkeypatch.setattr("src.llm._build_client", NoTextClient)

    assert parse_note("hi", TODAY) is None

    assert _outcome_entries(capsys)[0]["reason"] == "empty_response"


def test_parse_note_logs_bad_request_400_separately_from_other(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A 400 is how the API rejects unusable image bytes (corrupt, bad mime,
    # oversized) — worth telling apart from a generic failure.
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(raises=_api_error(400)))

    assert parse_image(b"x", "image/png", TODAY, caption="") is None

    assert _outcome_entries(capsys)[0]["reason"] == "bad_request_400"


# --- parse_image (multimodal Gemini adapter, mocked client) ---


def _inline_parts(contents: object) -> list[object]:
    """Return the parts of a multimodal ``contents`` list that carry image bytes."""
    if not isinstance(contents, list):
        return []
    return [p for p in contents if getattr(p, "inline_data", None) is not None]


def test_parse_image_returns_structured_expense_from_gemini_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps(
        {
            "date": "2026-07-10",
            "contract_name": "",
            "category": "Ingredients",
            "type": "Expense",
            "amount": 20000,
            "notes": "Meat purchase",
            "confidence": "high",
        }
    )
    client = FakeClient(text=body)
    monkeypatch.setattr("src.llm._build_client", lambda: client)

    note = parse_image(b"\x89PNG-bytes", "image/png", TODAY, caption="")

    assert note is not None
    assert note.type == "Expense"
    assert note.amount == "20000"
    # The image bytes + mime type must reach the model as an inline part, and
    # the current date must be in the prompt for relative-date resolution.
    parts = _inline_parts(client.last_kwargs.get("contents"))
    assert len(parts) == 1
    assert parts[0].inline_data.mime_type == "image/png"
    assert parts[0].inline_data.data == b"\x89PNG-bytes"
    assert TODAY.isoformat() in str(client.last_kwargs.get("contents"))


def test_parse_image_uses_caption_as_notes_fallback_for_sparse_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(text='{"amount": 5000}'))

    note = parse_image(b"bytes", "image/jpeg", TODAY, caption="Taxi to venue")

    assert note is not None
    assert note.amount == "5000"
    assert note.notes == "Taxi to venue"  # caption fills the empty notes field


def test_parse_image_returns_none_when_model_returns_non_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.llm._build_client", lambda: FakeClient(text="sorry, no receipt"))
    assert parse_image(b"bytes", "image/jpeg", TODAY, caption="") is None


def test_parse_image_returns_none_when_model_call_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.llm._build_client", lambda: FakeClient(raises=RuntimeError("api down"))
    )
    assert parse_image(b"bytes", "image/jpeg", TODAY, caption="") is None


# --- _build_client (adapter) ---


def test_build_client_raises_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        _build_client()


def test_build_client_passes_api_key_to_genai(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_client(*, api_key: str) -> object:
        captured["api_key"] = api_key
        return object()

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("src.llm.genai.Client", fake_client)

    assert _build_client() is not None
    assert captured["api_key"] == "test-key"
