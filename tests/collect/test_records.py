import gzip
import json
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from rebalance.collect import (
    DeadlineExceededError,
    build_record,
    day_path,
    encode_member,
    error_class,
    looks_like_json,
)

BASE_DIR = Path("data") / "live"


def ts(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: float = 0.0
) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp() + second


# error_class tests
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        pytest.param(
            HTTPError("https://x/", 403, "Forbidden", Message(), None),
            "http_403",
            id="http_error",
        ),
        pytest.param(TimeoutError(), "timeout", id="timeout"),
        pytest.param(
            URLError(TimeoutError()), "timeout", id="url_error_wrapping_timeout"
        ),
        pytest.param(URLError("no route"), "network", id="url_error"),
        pytest.param(ConnectionResetError(), "network", id="connection_reset"),
        pytest.param(DeadlineExceededError(), "deadline", id="deadline"),
        pytest.param(ValueError("x"), "ValueError", id="other_uses_type_name"),
    ],
)
def test_error_class_maps_exception_to_label(exc: BaseException, expected: str) -> None:
    assert error_class(exc) == expected


def test_error_class_label_does_not_contain_url() -> None:
    exc = HTTPError("https://x/?app_key=SECRET", 403, "Forbidden", Message(), None)

    label = error_class(exc)

    assert label == "http_403"
    assert "SECRET" not in label


# look_like_json tests
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param('{"a": 1}', False, id="object"),
        pytest.param("[1, 2, 3]", True, id="array"),
        pytest.param("   [1, 2, 3]   ", True, id="padded"),
        pytest.param('{"a": 1', False, id="truncated"),
        pytest.param('[{"a": 1}', False, id="truncated_array"),
        pytest.param("<html>", False, id="html"),
        pytest.param("", False, id="empty"),
    ],
)
def test_looks_like_json(body: str, expected: bool) -> None:
    assert looks_like_json(body) == expected


# build_record tests
@pytest.mark.parametrize(
    ("status", "error", "body", "payload_ok"),
    [
        pytest.param(200, None, "[1, 2]", True, id="success"),
        pytest.param(None, "timeout", None, False, id="no_status_no_body"),
        pytest.param(403, "http_403", "<html>No</html>", False, id="status_with_html"),
    ],
)
def test_build_record_fields(
    status: int | None, error: str | None, body: str | None, payload_ok: bool
) -> None:
    record = build_record(ts(2027, 1, 15, 8, 0, 0.5), status, 123, error, body)

    assert record == {
        "schema_version": 1,
        "fetched_at": "2027-01-15T08:00:00.500Z",
        "status": status,
        "elapsed_ms": 123,
        "error": error,
        "payload_ok": payload_ok,
        "payload": body,
    }
    assert record["status"] == status
    assert record["error"] == error
    assert record["payload_ok"] is payload_ok
    assert record["payload"] == body


def test_build_record_exact_schema() -> None:
    record = build_record(ts(2027, 1, 15, 8, 0, 0.5), 200, 123, None, "[1]")

    assert record == {
        "schema_version": 1,
        "fetched_at": "2027-01-15T08:00:00.500Z",
        "status": 200,
        "elapsed_ms": 123,
        "error": None,
        "payload_ok": True,
        "payload": "[1]",
    }


# test encode_member
@pytest.mark.parametrize(
    "record",
    [
        pytest.param(
            {
                "schema_version": 1,
                "fetched_at": "2027-09-27T00:00:00.123Z",
                "status": 200,
                "elapsed_ms": 123,
                "error": None,
                "payload_ok": True,
                "payload": {"a": 1},
            },
            id="success",
        ),
        pytest.param(
            {
                "schema_version": 1,
                "fetched_at": "2027-09-27T00:00:00.456Z",
                "status": None,
                "elapsed_ms": 456,
                "error": "TimeoutError",
                "payload_ok": False,
                "payload": None,
            },
            id="failure_no_status_no_body",
        ),
        pytest.param(
            {
                "schema_version": 1,
                "fetched_at": "2027-09-27T00:00:00.789Z",
                "status": 403,
                "elapsed_ms": 789,
                "error": None,
                "payload_ok": False,
                "payload": "<html>Forbidden</html>",
            },
            id="failure_with_status_and_body",
        ),
    ],
)
def test_encode_member(record: dict[str, object]) -> None:
    encoded = encode_member(record)
    assert isinstance(encoded, bytes)
    decompressed = gzip.decompress(encoded)
    assert decompressed.endswith(b"\n")
    lines = decompressed.splitlines()
    assert len(lines) == 1
    decoded_record = json.loads(lines[0])
    assert decoded_record == record


def test_encode_member_concatenated_members_decompress_to_two_lines() -> None:
    records: list[dict[str, int | dict[str, int]]] = [
        {"schema_version": 1, "payload": {"a": 1}},
        {"schema_version": 1, "payload": {"b": 2}},
    ]

    decompressed = gzip.decompress(
        b"".join(encode_member(record) for record in records)
    )

    assert [json.loads(line) for line in decompressed.splitlines()] == records


def test_encode_member_same_record_twice_gives_identical_bytes() -> None:
    record: dict[str, int | dict[str, int]] = {"schema_version": 1, "payload": {"a": 1}}

    assert encode_member(record) == encode_member(record)


def test_encode_member_payload_with_special_text_decompresses_to_one_line() -> None:
    record: dict[str, int | str] = {
        "schema_version": 1,
        "payload": 'line one\nline two with "quotes" and café',
    }

    decompressed = gzip.decompress(encode_member(record))

    assert decompressed.count(b"\n") == 1
    assert json.loads(decompressed) == record


# test day_path
@pytest.mark.parametrize(
    ("fetched_at", "filename"),
    [
        pytest.param(ts(2027, 1, 15, 8), "2027-01-15.jsonl.gz", id="morning"),
        pytest.param(
            ts(2027, 1, 15, 23, 59, 59.5), "2027-01-15.jsonl.gz", id="last_instant"
        ),
        pytest.param(
            ts(2027, 1, 16, 0), "2027-01-16.jsonl.gz", id="midnight_starts_new_day"
        ),
        pytest.param(
            ts(2027, 12, 31, 23, 59, 59), "2027-12-31.jsonl.gz", id="year_end"
        ),
    ],
)
def test_day_path_files_by_utc_date(fetched_at: float, filename: str) -> None:
    assert day_path(BASE_DIR, fetched_at) == BASE_DIR / filename
