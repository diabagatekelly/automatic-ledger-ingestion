from typing import Any

import pytest

from src.media import download_media


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
