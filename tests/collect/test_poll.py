import gzip
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rebalance.collect import API_URL, FetchResult, Settings, poll_once

KEY = "TESTKEY123"


def ts(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: float = 0.0
) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp() + second


T = ts(2027, 1, 15, 8, 0, 0.5)  # a fixed "now"


class Appends:
    """A fake append that records what it was asked to write."""

    def __init__(self) -> None:
        self.items: list[tuple[Path, bytes]] = []

    def __call__(self, path: Path, data: bytes) -> None:
        self.items.append((path, data))


def fake_fetch(
    result: FetchResult, calls: list[dict[str, object]] | None = None
) -> Callable[..., FetchResult]:
    """A fetch that returns ``result`` and records how it was called."""

    def fetch_fn(url: str, *, timeout_s: float, deadline_s: float) -> FetchResult:
        if calls is not None:
            calls.append({"url": url, "timeout_s": timeout_s, "deadline_s": deadline_s})
        return result

    return fetch_fn


def only_record(appends: Appends) -> tuple[Path, dict[str, object]]:
    """Assert exactly one member was appended, and decode it."""
    ((path, data),) = appends.items
    return path, json.loads(gzip.decompress(data))


def test_poll_once_success_writes_one_record_and_returns_true(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(api_key=KEY)
    appends = Appends()

    ok = poll_once(
        settings,
        now=lambda: T,
        fetch_fn=fake_fetch(FetchResult(200, "[1]", None, 120)),
        append=appends,
    )

    path, record = only_record(appends)
    assert ok is True
    assert path == settings.live_dir / "2027-01-15.jsonl.gz"
    assert record["status"] == 200
    assert record["payload"] == "[1]"
    assert record["payload_ok"] is True
    assert record["fetched_at"] == "2027-01-15T08:00:00.500Z"


def test_poll_once_failure_still_writes_a_record(
    make_settings: Callable[..., Settings],
) -> None:
    appends = Appends()

    ok = poll_once(
        make_settings(api_key=KEY),
        now=lambda: T,
        fetch_fn=fake_fetch(FetchResult(None, None, "timeout", 30_000)),
        append=appends,
    )

    _, record = only_record(appends)
    assert ok is False
    assert record["error"] == "timeout"
    assert record["payload"] is None
    assert record["payload_ok"] is False


def test_poll_once_reads_the_clock_before_fetching(
    make_settings: Callable[..., Settings],
) -> None:
    order: list[str] = []

    def fake_now() -> float:
        order.append("now")
        return T

    def fetch_fn(url: str, *, timeout_s: float, deadline_s: float) -> FetchResult:
        order.append("fetch")
        return FetchResult(200, "[1]", None, 1)

    poll_once(
        make_settings(api_key=KEY), now=fake_now, fetch_fn=fetch_fn, append=Appends()
    )

    assert order == ["now", "fetch"]


def test_poll_once_200_with_html_body(
    make_settings: Callable[..., Settings],
) -> None:
    appends = Appends()

    ok = poll_once(
        make_settings(api_key=KEY),
        now=lambda: T,
        fetch_fn=fake_fetch(FetchResult(200, "<html>oops</html>", None, 50)),
        append=appends,
    )

    _, record = only_record(appends)
    assert ok is False
    assert record["status"] == 200
    assert record["payload"] == "<html>oops</html>"
    assert record["payload_ok"] is False
    assert record["error"] is None


def test_poll_once_passes_url_and_timeouts_to_fetch(
    make_settings: Callable[..., Settings],
) -> None:
    calls: list[dict[str, object]] = []

    poll_once(
        make_settings(api_key=KEY, request_timeout_s=3.0, deadline_s=7.0),
        now=lambda: T,
        fetch_fn=fake_fetch(FetchResult(200, "[1]", None, 1), calls),
        append=Appends(),
    )

    assert calls == [
        {"url": f"{API_URL}?app_key={KEY}", "timeout_s": 3.0, "deadline_s": 7.0}
    ]


def test_poll_once_record_never_contains_the_key(
    make_settings: Callable[..., Settings],
) -> None:
    appends = Appends()

    poll_once(
        make_settings(api_key=KEY),
        now=lambda: T,
        fetch_fn=fake_fetch(FetchResult(200, "[1]", None, 1)),
        append=appends,
    )

    _, record = only_record(appends)
    assert KEY not in json.dumps(record)


def test_poll_once_files_record_by_request_start(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(api_key=KEY)
    appends = Appends()
    start = ts(2027, 1, 15, 23, 59, 59.5)  # request starts just before midnight UTC

    poll_once(
        settings,
        now=lambda: start,
        fetch_fn=fake_fetch(FetchResult(200, "[1]", None, 2000)),  # and takes 2 s
        append=appends,
    )

    path, record = only_record(appends)
    assert path == settings.live_dir / "2027-01-15.jsonl.gz"
    assert record["fetched_at"] == "2027-01-15T23:59:59.500Z"


def test_poll_once_propagates_append_errors(
    make_settings: Callable[..., Settings],
) -> None:
    def failing_append(path: Path, data: bytes) -> None:
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        poll_once(
            make_settings(api_key=KEY),
            now=lambda: T,
            fetch_fn=fake_fetch(FetchResult(200, "[1]", None, 1)),
            append=failing_append,
        )


def test_poll_once_default_append_writes_a_readable_day_file(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(api_key=KEY)

    poll_once(
        settings,
        now=lambda: T,
        fetch_fn=fake_fetch(FetchResult(200, "[1]", None, 1)),
    )

    day_file = settings.live_dir / "2027-01-15.jsonl.gz"
    lines = gzip.decompress(day_file.read_bytes()).splitlines()
    assert [json.loads(line)["payload"] for line in lines] == ["[1]"]
