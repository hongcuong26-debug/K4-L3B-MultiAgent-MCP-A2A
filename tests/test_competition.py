from pathlib import Path

import httpx2
import pytest

from student_agent.competition import submission_status, submit_artifact
from student_agent.config import Settings


def settings(tmp_path: Path):
    return Settings("https://competition.test", "test-key", "https://mcp.test", tmp_path)


def test_upload_uses_multipart_file_and_receipt(tmp_path, monkeypatch):
    artifact = tmp_path / "submission.zip"
    artifact.write_bytes(b"test-zip")
    original_client = httpx2.Client

    def handler(request):
        assert request.method == "POST"
        assert str(request.url) == "https://competition.test/api/v2/submissions"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert b'name="file"' in request.content
        assert b"test-zip" in request.content
        return httpx2.Response(202, json={"receipt": "receipt-a"})

    monkeypatch.setattr(
        httpx2,
        "Client",
        lambda **kwargs: original_client(transport=httpx2.MockTransport(handler), **kwargs),
    )
    assert submit_artifact(settings(tmp_path), artifact) == {"receipt": "receipt-a"}


def test_upload_rejection_is_not_silently_successful(tmp_path, monkeypatch):
    artifact = tmp_path / "submission.zip"
    artifact.write_bytes(b"test-zip")
    original_client = httpx2.Client
    monkeypatch.setattr(
        httpx2,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx2.MockTransport(lambda request: httpx2.Response(401)), **kwargs
        ),
    )
    with pytest.raises(RuntimeError, match="401"):
        submit_artifact(settings(tmp_path), artifact)


def test_status_uses_receipt_route(tmp_path, monkeypatch):
    original_client = httpx2.Client

    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v2/submissions/receipt-a"
        return httpx2.Response(200, json={"status": "scored"})

    monkeypatch.setattr(
        httpx2,
        "Client",
        lambda **kwargs: original_client(transport=httpx2.MockTransport(handler), **kwargs),
    )
    assert submission_status(settings(tmp_path), "receipt-a")["status"] == "scored"
