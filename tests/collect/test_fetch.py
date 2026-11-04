import socket
from collections.abc import Callable

import pytest
from conftest import FakeServer

from rebalance.collect import MASK, USER_AGENT, FetchResult, fetch

KEY = "TESTKEY123"


@pytest.fixture
def refused_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/ok?app_key={KEY}"  # nothing listens here now


def test_fetch_ok(fake_server: FakeServer) -> None:
    result = fetch(fake_server.url("/ok"), timeout_s=5.0, deadline_s=10.0)

    assert result == FetchResult(200, '[{"a": 1}]', None, result.elapsed_ms)


def test_fetch_sends_explicit_user_agent(fake_server: FakeServer) -> None:
    fetch(fake_server.url("/ok"), timeout_s=5.0, deadline_s=10.0)

    assert fake_server.seen[-1]["user_agent"] == USER_AGENT


def test_fetch_403_returns_status_label_and_body(fake_server: FakeServer) -> None:
    result = fetch(fake_server.url("/forbidden"), timeout_s=5.0, deadline_s=10.0)

    assert result.status == 403
    assert result.error == "http_403"
    assert result.body == "<html>Forbidden</html>"


def test_fetch_connection_refused_gives_network_label(refused_url: str) -> None:
    result = fetch(refused_url, timeout_s=5.0, deadline_s=10.0)

    assert result.status is None
    assert result.error == "network"
    assert result.body is None


def test_fetch_timeout_gives_timeout_label(fake_server: FakeServer) -> None:
    result = fetch(fake_server.url("/hang"), timeout_s=0.1, deadline_s=10.0)

    assert result.status is None
    assert result.error == "timeout"
    assert result.body is None


def stepping_clock(*values: float) -> Callable[[], float]:
    calls = iter(values)
    last = values[-1]

    def clock() -> float:
        nonlocal last
        last = next(calls, last)
        return last

    return clock


def test_fetch_deadline_exceeded(fake_server: FakeServer) -> None:
    result = fetch(
        fake_server.url("/large"),
        timeout_s=5.0,
        deadline_s=10.0,
        monotonic=stepping_clock(0.0, 100.0),
    )

    assert result.status == 200
    assert result.error == "deadline"
    assert result.body is None


def test_fetch_truncated_body_gives_network_label(fake_server: FakeServer) -> None:
    result = fetch(fake_server.url("/truncated"), timeout_s=5.0, deadline_s=10.0)

    assert result.status == 200
    assert result.error == "network"
    assert result.body is None


def test_fetch_bad_utf8_does_not_raise(fake_server: FakeServer) -> None:
    result = fetch(fake_server.url("/baddata"), timeout_s=5.0, deadline_s=10.0)

    assert result.status == 200
    assert result.error is None
    assert result.body == "[\ufffd\ufffd]"


def test_fetch_empty_body(fake_server: FakeServer) -> None:
    result = fetch(fake_server.url("/empty"), timeout_s=5.0, deadline_s=10.0)

    assert result.status == 200
    assert result.error is None
    assert result.body == ""


def test_fetch_large_body_arrives_complete(fake_server: FakeServer) -> None:
    result = fetch(fake_server.url("/large"), timeout_s=5.0, deadline_s=10.0)

    assert result.status == 200
    assert result.error is None
    assert result.body is not None
    assert result.body.startswith("[1,1,1,1,1,1,1,1,1,1")
    assert result.body.endswith("1]")
    assert len(result.body) == 200_003


def test_fetch_invalid_url_returns_label_and_no_leak() -> None:
    result = fetch(f"not a url?app_key={KEY}", timeout_s=5.0, deadline_s=10.0)

    assert result.status is None
    assert result.error == "ValueError"
    assert result.body is None


def test_fetch_403_body_is_redacted(fake_server: FakeServer) -> None:
    result = fetch(
        fake_server.url(f"/echo?app_key={KEY}"), timeout_s=5.0, deadline_s=10.0
    )

    assert result.status == 403
    assert KEY not in (result.body or "")
    assert result.body == f"/echo?app_key={MASK}"


def test_fetch_elapsed_ms_from_monotonic_clock(fake_server: FakeServer) -> None:
    result = fetch(
        fake_server.url("/ok"),
        timeout_s=5.0,
        deadline_s=10.0,
        monotonic=stepping_clock(100.0, 100.5),
    )

    assert result.elapsed_ms == 500


def test_fetch_result_never_contains_the_key(
    fake_server: FakeServer, refused_url: str
) -> None:
    urls = [
        refused_url,
        f"not a url?app_key={KEY}",
        fake_server.url(f"/echo?app_key={KEY}"),
    ]
    for url in urls:
        result = fetch(url, timeout_s=5.0, deadline_s=10.0)

        assert KEY not in repr(result)
