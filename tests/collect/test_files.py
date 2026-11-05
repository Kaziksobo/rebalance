import gzip
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from rebalance.collect import (
    AlreadyRunningError,
    acquire_lock,
    append_member,
    encode_member,
    read_status,
    update_status,
    write_status,
)

CHILD = """
import sys, time
from pathlib import Path
from rebalance.collect import acquire_lock

with acquire_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    time.sleep(60)
"""


# append_member tests
def test_append_member_creates_missing_parent_folders(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "day.jsonl.gz"

    append_member(path, encode_member({"n": 1}))

    assert path.is_file()


def test_append_member_writes_exactly_the_bytes_given(tmp_path: Path) -> None:
    path = tmp_path / "day.jsonl.gz"
    data = encode_member({"n": 1})

    append_member(path, data)

    assert path.read_bytes() == data


def test_append_member_appends_and_keeps_earlier_members(tmp_path: Path) -> None:
    path = tmp_path / "day.jsonl.gz"

    append_member(path, encode_member({"n": 1}))
    append_member(path, encode_member({"n": 2}))

    lines = gzip.decompress(path.read_bytes()).splitlines()
    assert [json.loads(line) for line in lines] == [{"n": 1}, {"n": 2}]


def test_append_member_propagates_os_errors(tmp_path: Path) -> None:
    with pytest.raises((IsADirectoryError, PermissionError)):
        append_member(tmp_path, b"x")


def test_append_member_flushes_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(os, "fsync", calls.append)

    append_member(tmp_path / "day.jsonl.gz", b"x")

    assert len(calls) == 1


# update_status tests
@pytest.mark.parametrize(
    ("outcomes", "successes", "failures", "consecutive"),
    [
        pytest.param([True], 1, 0, 0, id="one_success"),
        pytest.param([False], 0, 1, 1, id="one_failure"),
        pytest.param([True, False, False], 1, 2, 2, id="failures_after_success"),
        pytest.param([False, False, True], 1, 2, 0, id="success_resets_consecutive"),
    ],
)
def test_update_status_counts(
    outcomes: list[bool], successes: int, failures: int, consecutive: int
) -> None:
    status = None
    for i, ok in enumerate(outcomes):
        status = update_status(status, ok, now=1000.0 + i)

    assert status is not None
    assert status["successes"] == successes
    assert status["failures"] == failures
    assert status["consecutive_failures"] == consecutive


def test_update_status_schema_contract() -> None:
    status = update_status(None, True, now=1000.0)
    status = update_status(status, False, now=1001.0)

    assert status == {
        "last_success": 1000.0,
        "last_attempt": 1001.0,
        "successes": 1,
        "failures": 1,
        "consecutive_failures": 1,
    }


def test_update_status_first_failure_has_no_last_success() -> None:
    status = update_status(None, False, now=1000.0)

    assert status == {
        "last_success": None,
        "last_attempt": 1000.0,
        "successes": 0,
        "failures": 1,
        "consecutive_failures": 1,
    }


def test_update_status_tolerates_empty_previous_status() -> None:
    status = update_status({}, True, now=1000.0)

    assert status["successes"] == 1
    assert status["last_success"] == 1000.0
    assert status["last_attempt"] == 1000.0


def test_update_status_does_not_mutate_previous_dict() -> None:
    first = update_status(None, True, now=1000.0)
    snapshot = dict(first)

    update_status(first, False, now=1001.0)

    assert first == snapshot


def test_update_status_failure_does_not_change_last_success() -> None:
    status = None
    status = update_status(status, True, now=1000.0)
    status = update_status(status, False, now=1001.0)

    assert status["last_success"] == 1000.0
    assert status["failures"] == 1
    assert status["consecutive_failures"] == 1


def test_update_status_ignores_non_numeric_values() -> None:
    status = update_status({"successes": "x", "last_success": "y"}, False, now=1000.0)

    assert status["successes"] == 0
    assert status["last_success"] is None


# read_status tests
@pytest.mark.parametrize(
    "content",
    [
        pytest.param(None, id="missing"),
        pytest.param(b"", id="empty"),
        pytest.param(b" ", id="whitespace"),
        pytest.param(b"{", id="corrupt_json"),
        pytest.param(b"[]", id="json_list"),
        pytest.param(b"42", id="bare_number"),
        pytest.param(b"null", id="null"),
        pytest.param(b"\xff", id="invalid_utf8"),
    ],
)
def test_read_status_returns_none_for_bad_inputs(
    tmp_path: Path, content: bytes | None
) -> None:
    path = tmp_path / "status.json"
    if content is not None:
        path.write_bytes(content)

    assert read_status(path) is None


def test_read_status_returns_dict_for_valid_json(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    expected_status: dict[str, float] = {
        "successes": 1,
        "failures": 2,
        "consecutive_failures": 3,
        "last_success": 1000.0,
    }
    path.write_text(json.dumps(expected_status))

    status = read_status(path)
    assert status == expected_status


# write_status tests
def test_write_status_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "status.json"
    expected_status: dict[str, float] = {
        "successes": 1,
        "failures": 2,
        "consecutive_failures": 3,
        "last_success": 1000.0,
    }

    write_status(path, expected_status)
    status = read_status(path)

    assert status == expected_status


def test_write_status_overwrites_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    initial_status: dict[str, float] = {
        "successes": 1,
        "failures": 0,
        "consecutive_failures": 0,
        "last_success": 1000.0,
    }
    updated_status: dict[str, float] = {
        "successes": 2,
        "failures": 1,
        "consecutive_failures": 1,
        "last_success": 1001.0,
    }

    write_status(path, initial_status)
    write_status(path, updated_status)
    status = read_status(path)

    assert status == updated_status


def test_write_status_creates_parent_folder(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "status.json"
    status: dict[str, float] = {
        "successes": 1,
        "failures": 0,
        "consecutive_failures": 0,
        "last_success": 1000.0,
    }

    write_status(path, status)
    assert path.exists()


def test_write_status_leaves_no_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    status: dict[str, float] = {
        "successes": 1,
        "failures": 0,
        "consecutive_failures": 0,
        "last_success": 1000.0,
    }

    write_status(path, status)

    # Check for any temp files in the same directory
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_write_status_failure_keeps_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "status.json"
    initial: dict[str, float] = {"successes": 1, "last_success": 1000.0}
    write_status(path, initial)

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("simulated failure")

    monkeypatch.setattr(os, "replace", fail)

    with pytest.raises(OSError, match="simulated failure"):
        write_status(path, {"successes": 2, "last_success": 1001.0})

    assert read_status(path) == initial
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


# acquire_lock tests
def test_acquire_lock_creates_file_and_parent_folders(tmp_path: Path) -> None:
    path = tmp_path / "a" / ".logger.lock"

    with acquire_lock(path):
        assert path.is_file()


def test_acquire_lock_second_acquire_while_held_raises(tmp_path: Path) -> None:
    path = tmp_path / ".logger.lock"

    with acquire_lock(path), pytest.raises(AlreadyRunningError), acquire_lock(path):
        pass


def test_acquire_lock_can_be_reacquired_after_exit(tmp_path: Path) -> None:
    path = tmp_path / ".logger.lock"
    with acquire_lock(path):
        pass

    with acquire_lock(path):
        pass


def test_acquire_lock_is_released_when_body_raises(tmp_path: Path) -> None:
    path = tmp_path / ".logger.lock"
    with pytest.raises(RuntimeError), acquire_lock(path):
        raise RuntimeError("boom")

    with acquire_lock(path):
        pass


def acquire_eventually(path: Path, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with acquire_lock(path):
                return
        except AlreadyRunningError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def test_acquire_lock_is_released_when_holder_process_is_killed(tmp_path: Path) -> None:
    path = tmp_path / ".logger.lock"
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(path)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"

        with pytest.raises(AlreadyRunningError), acquire_lock(path):
            pass

        child.kill()
        child.wait(timeout=10)

        acquire_eventually(path)  # the OS freed it when the holder died
        pass
    finally:
        child.kill()
        child.wait(timeout=10)
        if child.stdout is not None:
            child.stdout.close()
