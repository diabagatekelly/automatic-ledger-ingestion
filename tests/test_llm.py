import json
from datetime import date

import pytest

from src.llm import ParsedNote, _build_client, coerce_note, parse_note

TODAY = date(2026, 7, 13)


class FakeResponse:
    def __init__(self, text: str) -> None:
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


# --- coerce_note (pure) ---


def test_coerce_note_maps_a_full_valid_payload() -> None:
    note = coerce_note(
        {
            "date": "2026-07-13",
            "contract_name": "Wedding Cake",
            "category": "Revenue",
            "type": "Revenue",
            "amount": 200,
            "notes": "Cash sale",
            "confidence": "high",
        },
        raw_text="Cash sale, $200, Wedding Cake",
        today=TODAY,
    )
    assert note == ParsedNote(
        date="2026-07-13",
        contract_name="Wedding Cake",
        category="Revenue",
        type="Revenue",
        amount="200",
        notes="Cash sale",
        confidence="high",
    )


def test_coerce_note_formats_a_decimal_amount() -> None:
    note = coerce_note({"amount": 49.5}, raw_text="x", today=TODAY)
    assert note.amount == "49.5"


def test_coerce_note_defaults_missing_fields() -> None:
    note = coerce_note({}, raw_text="Cash sale, $200", today=TODAY)
    assert note.date == "2026-07-13"  # falls back to today
    assert note.contract_name == ""
    assert note.category == ""
    assert note.type == ""
    assert note.amount == ""
    assert note.notes == "Cash sale, $200"  # falls back to the raw text
    assert note.confidence == "low"  # unknown → treated as low


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
