import json
from typing import Any

import pytest
import requests

from src.media import classify_download_error, download_media, log_download_failure


class FakeHTTPResponse:
    """Minimal stand-in for a ``requests.Response``."""

    def __init__(self, *, json_body: Any = None, content: bytes = b"") -> None:
        self._json = json_body
        self.content = content
        self.raised = False

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        self.raised = True


class FakeRequests:
    """Records GET calls and returns canned responses in order."""

    def __init__(self, responses: list[FakeHTTPResponse]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeHTTPResponse:
        self.calls.append({"url": url, **kwargs})
        return self._responses[len(self.calls) - 1]


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "test-token")


def test_download_media_returns_bytes_and_mime_type(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = {"url": "https://lookaside/media", "mime_type": "image/png"}
    fake = FakeRequests(
        [
            FakeHTTPResponse(json_body=metadata),
            FakeHTTPResponse(content=b"\x89PNG-bytes"),
        ]
    )
    monkeypatch.setattr("src.media.requests", fake)

    data, mime_type = download_media("media-1")

    assert data == b"\x89PNG-bytes"
    assert mime_type == "image/png"


def test_download_media_defaults_mime_type_when_metadata_omits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRequests(
        [
            FakeHTTPResponse(json_body={"url": "https://lookaside/media"}),  # no mime_type
            FakeHTTPResponse(content=b"raw-bytes"),
        ]
    )
    monkeypatch.setattr("src.media.requests", fake)

    data, mime_type = download_media("media-1")

    assert data == b"raw-bytes"
    assert mime_type == "application/octet-stream"


def test_download_media_hits_metadata_then_binary_url_with_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = {"url": "https://lookaside/media", "mime_type": "image/jpeg"}
    fake = FakeRequests(
        [
            FakeHTTPResponse(json_body=metadata),
            FakeHTTPResponse(content=b"jpeg-bytes"),
        ]
    )
    monkeypatch.setattr("src.media.requests", fake)

    download_media("media-42")

    # First GET resolves the media ID to a temporary download URL.
    assert fake.calls[0]["url"].endswith("/media-42")
    # Second GET fetches the bytes from that URL. Both carry the access token.
    assert fake.calls[1]["url"] == "https://lookaside/media"
    for call in fake.calls:
        assert call["headers"]["Authorization"] == "Bearer test-token"


def test_download_media_raises_without_access_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="WHATSAPP_ACCESS_TOKEN"):
        download_media("media-1")


def test_download_media_raises_when_metadata_has_no_url(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRequests([FakeHTTPResponse(json_body={"mime_type": "image/jpeg"})])
    monkeypatch.setattr("src.media.requests", fake)
    with pytest.raises(RuntimeError, match="no download URL"):
        download_media("media-1")


# --- media-download failure telemetry (Issue #44) ---
# A download failure never reaches Gemini, so it is invisible to the
# gemini_parse telemetry — it gets its own event with its own reason buckets.


def _http_error(status_code: int) -> requests.HTTPError:
    """Build a requests.HTTPError carrying a response with the given status."""
    response = requests.Response()
    response.status_code = status_code
    return requests.HTTPError(f"{status_code} error", response=response)


def test_classify_download_error_buckets_every_failure_mode() -> None:
    # auth_401 is split out deliberately: it's the token-expiry signature (#5).
    assert classify_download_error(_http_error(401)) == "auth_401"
    assert classify_download_error(_http_error(404)) == "not_found"
    # Graph resolves an unknown/malformed media id as a 400 GraphMethodException.
    assert classify_download_error(_http_error(400)) == "not_found"
    assert classify_download_error(_http_error(500)) == "other"
    assert classify_download_error(requests.Timeout("slow")) == "timeout"
    # Subclasses (connect/read timeouts) bucket the same.
    assert classify_download_error(requests.ConnectTimeout("slow")) == "timeout"
    # An HTTPError without an attached response can't be bucketed by status.
    assert classify_download_error(requests.HTTPError("bare")) == "other"
    assert classify_download_error(RuntimeError("metadata has no download URL")) == "other"


def test_log_download_failure_emits_one_structured_warning_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_download_failure(_http_error(401))

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1
    entry = lines[0]
    # Assert the full structured-log contract, not just the reason: this is the
    # schema Cloud Run parses into jsonPayload and the log-based metric counts.
    assert entry["severity"] == "WARNING"
    assert entry["event"] == "media_download"
    assert entry["outcome"] == "failure"
    assert entry["reason"] == "auth_401"
    assert entry["message"].startswith("media_download outcome=failure")


def test_log_download_failure_reason_follows_the_classifier(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_download_failure(requests.Timeout("slow"))

    entry = json.loads(capsys.readouterr().out.strip())
    assert entry["reason"] == "timeout"
