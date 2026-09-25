import io

import pytest

from student_agent.http_session import HttpSession


def test_sse_ignores_notifications_and_correlates_response():
    stream = io.BytesIO(
        b': heartbeat\r\n\r\ndata: {"jsonrpc":"2.0","method":"notice"}\r\n\r\n'
        b'event: message\r\ndata: {"jsonrpc":"2.0",\r\n'
        b'data: "id":2,"result":{"tools":[]}}\r\n\r\n'
    )
    assert HttpSession._read_sse(stream, 2)["result"] == {"tools": []}


def test_sse_cannot_substitute_another_request():
    stream = io.BytesIO(b'data: {"id":3,"result":{}}\n\n')
    with pytest.raises(ValueError, match="without a matching"):
        HttpSession._read_sse(stream, 2)


def test_sse_has_response_size_limit():
    stream = io.BytesIO(b":" + b"x" * (1024 * 1024) + b"\n")
    with pytest.raises(ValueError, match="exceeds"):
        HttpSession._read_sse(stream, 1)


def test_transport_does_not_forward_credentials_on_redirect():
    session = HttpSession("https://example.test", "test-key")
    try:
        assert not session._client.follow_redirects
    finally:
        session.close()
