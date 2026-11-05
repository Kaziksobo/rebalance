"""Single-file logger for the TfL BikePoint live feed.

Polls the API once a minute and appends each response, unchanged, to a gzipped
JSON-lines file for that UTC day. The payload is stored raw on purpose: what
matters is decided later in analysis, not at collection time.

Constraints that shape this file:
    * Standard library only, and nothing imported from the rebalance package.
      It is copied to a Raspberry Pi on its own and run with the system Python.
    * The API key travels in the request URL, so no URL or exception text may
      ever be logged or stored (see redact() and fetch()).
    * It runs unattended. A failed poll is recorded and the loop carries on;
      only another running instance, an implausible clock or a full disk stop it.

Layout: constants, settings, pure helpers (redaction, ticks, records,
encoding), I/O (fetch, files, lock), then the poll loop and entry point.

Exit codes: 0 clean stop, 1 runtime failure, 2 configuration error,
3 another instance is already running.
"""

import gzip
import json
import math
import os
import re
import sys
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.client import HTTPException, IncompleteRead
from json import JSONDecodeError
from pathlib import Path
from typing import BinaryIO, TypedDict, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

# --- Constants and exceptions ---

MASK = "***"
# Masks the value of any app_key parameter, even one that isn't the key we hold,
# so a stale or wrong key can't leak either. The value ends at &, whitespace,
# a quote, a bracket or a comma.
_APP_KEY_VALUE = re.compile(r"(app_key=)[^&\s'\"),]*", re.IGNORECASE)


# The per-operation timeout bounds each blocking socket call; the deadline bounds
# the whole request. urllib's timeout alone doesn't cap total time, so both are needed.
DEFAULT_INTERVAL_S = 60.0
DEFAULT_REQUEST_TIMEOUT_S = 10.0
DEFAULT_DEADLINE_S = 30.0

# The only two settings the logger reads; anything else in .env is ignored.
_SETTING_NAMES = ("TFL_APP_KEY", "REBALANCE_DATA_DIR")

# A Pi has no real-time clock: before NTP syncs, the time can be wildly wrong.
# Anything earlier than this means the clock hasn't synced.
CLOCK_FLOOR = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
CLOCK_TOLERANCE_S = 60.0  # Allows small backwards steps from NTP corrections.

# Bump when the record format changes, so old day files stay interpretable.
SCHEMA_VERSION = 1

# TfL's front end returned 403 for urllib's default agent, so an explicit one is needed.
USER_AGENT = "rebalance-logger/0.1 (student project)"

# 64 KiB: small enough to check the deadline often, large enough to be efficient on a
# slow CPU. Error bodies are diagnostic only, so cap what we keep.
CHUNK_SIZE = 64 * 1024
MAX_ERROR_BODY = 64 * 1024


class DeadlineExceededError(Exception):
    """Raised when one request runs past its total deadline."""


class ConfigError(Exception):
    """Raised for bad configuration. Messages must never contain values."""


class AlreadyRunningError(Exception):
    """Raised when another logger instance holds the lock."""


# --- Settings ---


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the logger needs to run, resolved once at start-up.

    Attributes:
        api_key: TfL API key. Excluded from repr so it cannot leak into logs.
        data_dir: Absolute path to the data directory.
        interval_s: Seconds between polls.
        request_timeout_s: Timeout for each blocking socket operation.
        deadline_s: Total time allowed for one request, checked on a
            monotonic clock.

    Raises:
        ConfigError: If key is missing or blank, or there is a non-positive interval.
    """

    # repr=False keeps the key out of any printed or logged Settings.
    api_key: str = field(repr=False)
    data_dir: Path
    interval_s: float = DEFAULT_INTERVAL_S
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S
    deadline_s: float = DEFAULT_DEADLINE_S

    def __post_init__(self) -> None:
        """Reject values the logger cannot run with."""
        if not self.api_key:
            raise ConfigError("TFL_APP_KEY is not set")
        if self.interval_s <= 0:
            raise ConfigError("interval must be positive")
        if not 0 < self.request_timeout_s <= self.deadline_s:
            raise ConfigError("timeouts must satisfy 0 < request <= deadline")

    @property
    def live_dir(self) -> Path:
        """Directory holding the day files, status file, lock and log."""
        return self.data_dir / "live"

    @property
    def lock_path(self) -> Path:
        """Path of the single-instance lock file."""
        return self.live_dir / ".logger.lock"

    @property
    def status_path(self) -> Path:
        """Path of the status file."""
        return self.live_dir / ".status.json"

    @property
    def log_path(self) -> Path:
        """Path of the logger's own log."""
        return self.live_dir / "logger.log"


def find_env_file(start: Path, max_levels: int = 4) -> Path | None:
    """Find a .env file in the current or parent directories.

    Will search up to `max_levels` parent directories.
    Returns the first .env file found, or None if not found.

    Args:
        start: The starting directory to search from.
        max_levels: The maximum number of parent directories to search.

    Returns:
        The path to the .env file if found, otherwise None.
    """
    current = start.resolve()
    for _ in range(max_levels + 1):
        env_path = current / ".env"
        if env_path.is_file():
            return env_path
        if current.parent == current:
            break  # Reached the root directory
        current = current.parent
    return None


def load_settings(
    search_from: Path,
    environ: Mapping[str, str],
    *,
    interval_s: float = DEFAULT_INTERVAL_S,
) -> Settings:
    """Load settings from a .env file and environment variables.

    Finds .env file, reads it, parses it, overrides with env vars,
    treats empty values in env as unset so falls back to file.
    Resolves data_dir to absolute path

    Args:
        search_from (Path): The directory to start searching for a .env file.
        environ (Mapping[str, str]): The environment variables to use.
        interval_s (float, optional): The interval between polls.
                                      Defaults to DEFAULT_INTERVAL_S.

    Returns:
        Settings: The loaded settings.
    """
    env_file_path = find_env_file(search_from)
    file_vars: dict[str, str] = {}
    if env_file_path:
        # utf-8-sig drops the byte-order mark some Windows editors add.
        file_text = env_file_path.read_text(encoding="utf-8-sig")
        file_vars = parse_env_file(file_text)

    # Environment beats the file; an empty environment value counts as unset.
    values = {
        name: environ.get(name) or file_vars.get(name, "") for name in _SETTING_NAMES
    }

    # A relative data_dir is anchored to the .env's folder, never the working directory,
    # which differs under systemd and Task Scheduler.
    base_dir = env_file_path.parent if env_file_path else search_from
    data_dir = (base_dir / (values["REBALANCE_DATA_DIR"] or "data")).resolve()

    return Settings(
        api_key=values["TFL_APP_KEY"],
        data_dir=data_dir,
        interval_s=interval_s,
    )


def parse_env_file(text: str) -> dict[str, str]:
    """Parse a .env file into a dictionary.

    Skips empty lines and comments.
    Splits on the first '=' to allow values with '=' in them.
    Inline comments are not stripped, so values can contain '#' if needed.

    Args:
        text: The text of the .env file.

    Returns:
        A dictionary of key-value pairs.

    Raises:
        ConfigError: If a line is invalid (does not contain '=').
    """
    env_dict: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"Invalid line {line_number} in .env file")
        # Split on the first '=' to allow values with '=' in them
        key, value = line.split("=", 1)
        # strip whitespace around key and value
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        env_dict[key] = value
    return env_dict


# --- Redaction ---
def redact(text: str, key: str) -> str:
    """Redact the given key from the text.

    Encoded form: a key with reserved characters appears URL-encoded inside URLs.
    An empty key is skipped, since replacing "" would insert the mask between
    every character.

    Args:
        text: The text to redact the key from.
        key: The key to redact.

    Returns:
        The text with the key redacted.
    """
    if key:
        for form in (quote(key, safe=""), key):
            text = text.replace(form, MASK)
    # Safety net: mask any app_key value, whatever it is.
    return _APP_KEY_VALUE.sub(rf"\1{MASK}", text)


# --- Scheduling ---
def first_tick(now: float, interval_s: float) -> float:
    """Return the first tick time at or after ``now``, on the interval grid.

    Args:
        now: Current time, epoch seconds.
        interval_s: Seconds between ticks.

    Returns:
        Absolute epoch time of the first grid point at or after ``now``.

    Raises:
        ValueError: If ``interval_s`` is not positive.
    """
    # The grid is anchored at the epoch, so a 60 s interval ticks on whole minutes.
    if interval_s <= 0:
        raise ValueError("interval must be positive")

    count = math.ceil(now / interval_s)
    return count * interval_s


def next_tick(previous: float, interval_s: float, now: float) -> float:
    """Return the first scheduled tick strictly after ``now``.

    Ticks lie on ``previous + k * interval_s``. Missed ticks are skipped,
    never returned, so a long sleep produces one tick, not a burst.

    Raises:
        ValueError: If ``interval_s`` is not positive.
    """
    if interval_s <= 0:
        raise ValueError("interval must be positive")
    elapsed = now - previous
    # A clock that went backwards still returns `previous + interval_s`,
    # never an earlier time.
    # +1 makes the result strictly after now; the guard below covers float rounding.
    k = max(math.floor(elapsed / interval_s) + 1, 1)
    result = previous + k * interval_s
    if result <= now:
        result += interval_s
    return result


def clock_plausible(now: float, last_record: float | None) -> bool:
    """Return True if the system clock looks sane.

    The clock must not be before CLOCK_FLOOR, and must not be earlier than
    the last record by more than CLOCK_TOLERANCE_S. A clock far *ahead* of the
    last record is normal after an outage and is accepted.
    """
    if now < CLOCK_FLOOR:
        return False
    return not (last_record is not None and now < last_record - CLOCK_TOLERANCE_S)


# --- Records and encoding ---
def error_class(exc: BaseException) -> str:
    """Map an exception to a label for logging and metrics.

    The mapping is as follows:
    1. HTTPError first (it subclasses URLError)      -> f"http_{exc.code}"
    2. DeadlineExceededError                          -> "deadline"
    3. TimeoutError                                   -> "timeout"
    4. URLError: if isinstance(exc.reason, TimeoutError) -> "timeout", else "network"
    5. any other OSError                              -> "network"
    6. everything else                                -> type(exc).__name__

    Args:
        exc (BaseException): The exception to map.

    Returns:
        str: The label for the exception.
    """
    # HTTPError subclasses URLError and OSError, so it must be checked first.
    if isinstance(exc, HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, DeadlineExceededError):
        return "deadline"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, URLError):
        if isinstance(exc.reason, TimeoutError):
            return "timeout"
        return "network"
    # IncompleteRead comes from http.client and isn't an OSError.
    if isinstance(exc, (OSError, HTTPException)):
        return "network"
    return type(exc).__name__


def looks_like_json(body: str) -> bool:
    """Check if a string looks like JSON.

    Args:
        body (str): The string to check.

    Returns:
        bool: True if the string looks like JSON, False otherwise.
    """
    text = body.strip()
    return text.startswith("[") and text.endswith("]")


def build_record(
    fetched_at: float,
    status: int | None,
    elapsed_ms: int,
    error: str | None,
    body: str | None,
) -> dict[str, object]:
    """Build a record dictionary for logging.

    Args:
        fetched_at (float): The timestamp when the data was fetched.
        status (int | None): The HTTP status code of the response.
        elapsed_ms (int): The elapsed time in milliseconds for the request.
        error (str | None): The error label, if any.
        body (str | None): The response body, if any.

    Returns:
        dict[str, object]: The record dictionary.
    """
    iso = datetime.fromtimestamp(fetched_at, UTC).isoformat(timespec="milliseconds")
    iso = iso.replace("+00:00", "Z")
    # payload is the raw response text, never parsed: parsing 2 MB each minute is slow
    # on the Pi, and raw text is guaranteed untouched.
    # payload_ok is only a cheap shape check.
    return {
        "schema_version": SCHEMA_VERSION,
        "fetched_at": iso,
        "status": status,
        "elapsed_ms": elapsed_ms,
        "error": error,
        "payload_ok": body is not None and looks_like_json(body),
        "payload": body,
    }


def encode_member(record: Mapping[str, object]) -> bytes:
    """Encode a record as a single line of JSON, then compress with gzip.

    Args:
        record (Mapping[str, object]): The record to encode.

    Returns:
        bytes: The gzip-compressed JSON line.
    """
    # One JSON object per line, compressed as its own gzip member. Members concatenate
    # into a valid gzip stream, so appending is safe and a crash costs at most the last
    # poll. mtime=0 makes the output deterministic.
    line = json.dumps(record, separators=(",", ":")) + "\n"
    return gzip.compress(line.encode("utf-8"), mtime=0)


def day_path(base: Path, fetched_at: float) -> Path:
    """Return the path to the day's file for the given timestamp.

    Args:
        base (Path): The base directory for the data.
        fetched_at (float): The timestamp to determine the day.

    Returns:
        Path: The path to the day's file.
    """
    date = datetime.fromtimestamp(fetched_at, UTC).strftime("%Y-%m-%d")
    return base / f"{date}.jsonl.gz"


# --- Fetching ---
@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of one request. Never contains the URL or the key.

    Attributes:
        status: HTTP status if a response started, otherwise None.
        body: Response text, or None if no usable body.
        error: Error label from error_class, or None on success.
        elapsed_ms: Wall time of the request in milliseconds.
    """

    status: int | None
    body: str | None
    error: str | None
    elapsed_ms: int


def fetch(
    url: str,
    *,
    timeout_s: float,
    deadline_s: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> FetchResult:
    """Fetch a URL, never raising for expected failures.

    Failures are returned as an error label, never raised.

    Args:
        url: Full URL, including any key. Never logged or returned.
        timeout_s: Timeout for each blocking socket operation.
        deadline_s: Total time allowed, checked on a monotonic clock.
        monotonic: Clock function, injectable for tests.

    Returns:
        A FetchResult describing the outcome.
    """
    start: float = monotonic()
    status: int | None = None
    try:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=timeout_s) as response:
            status = response.status
            expected: str | None = response.headers.get("Content-Length")
            chunks: list[bytes] = []
            while True:
                chunk: bytes = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
                # urllib's timeout only covers each socket call,
                # so check total elapsed after every chunk.
                if monotonic() - start > deadline_s:
                    raise DeadlineExceededError()
            # read(n) returns short without raising if the connection closes early,
            # so compare against Content-Length ourselves.
            data: bytes = b"".join(chunks)
            if expected is not None and len(data) != int(expected):
                raise IncompleteRead(data, int(expected))
        return FetchResult(
            status,
            data.decode("utf-8", errors="replace"),
            None,
            int((monotonic() - start) * 1000),
        )
    except HTTPError as exc:
        # Error pages can echo the request URL, key included, so the
        # body is redacted before it is stored.
        try:
            body_bytes: bytes = exc.read(MAX_ERROR_BODY)
        except Exception:
            body_bytes = b""
        finally:
            exc.close()
        body: str = redact(body_bytes.decode("utf-8", errors="replace"), "")
        return FetchResult(
            exc.code, body, error_class(exc), int((monotonic() - start) * 1000)
        )
    except Exception as exc:
        # Last resort, so no failure can end the loop. Only the label is recorded,
        # never str(exc), which can contain the URL.
        return FetchResult(
            status, None, error_class(exc), int((monotonic() - start) * 1000)
        )


# --- Files and locking ---
def append_member(path: Path, data: bytes) -> None:
    """Write a gzip member to the given file, creating it if needed.

    One call is one whole member; flushed and fsynced; a crash leaves at
    most one partial member; raises OSError

    Args:
        path (Path): The path to the file to append to.
        data (bytes): The gzip-compressed data to append.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


class Status(TypedDict):
    """The shape of the status file the logger writes.

    Last success and last attempt are epoch seconds, or None if never.
    """

    last_success: float | None
    last_attempt: float
    successes: int
    failures: int
    consecutive_failures: int


def _count(value: object) -> int:
    """Return ``value`` if it is a real integer, else 0 (a damaged file)."""
    # bool is a subclass of int, so exclude it explicitly.
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _timestamp(value: object) -> float | None:
    """Return ``value`` as a float if it is a real number, else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def update_status(
    previous: Mapping[str, object] | None, ok: bool, now: float
) -> Status:
    """Return a new status after one poll; ``previous`` is never modified.

    Returns a new dict, never modifies previous; missing or damaged values count as
    zero, so a corrupt status file can't stop the logger at start-up.

    Args:
        previous (Mapping[str, object] | None): The previous status, or None if there is
                                                no previous status.
        ok (bool): Whether the last poll was successful.
        now (float): The current timestamp.

    Returns:
        Status: The new status dictionary.
    """
    base = previous or {}
    return {
        "last_success": now if ok else _timestamp(base.get("last_success")),
        "last_attempt": now,
        "successes": _count(base.get("successes")) + (1 if ok else 0),
        "failures": _count(base.get("failures")) + (0 if ok else 1),
        "consecutive_failures": 0
        if ok
        else _count(base.get("consecutive_failures")) + 1,
    }


def read_status(path: Path) -> dict[str, object] | None:
    """Read the status file and return its contents as a dictionary.

    If the file does not exist or is not valid JSON, return None.
    An advisory file; returns None for anything unusable.

    Args:
        path (Path): Path to the status file.

    Returns:
        dict[str, object] | None: The status dictionary if the file exists and is valid
                                  JSON, return None if the file is missing, unreadable,
                                  not valid JSON, or not a JSON object.
    """
    try:
        text: str = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    try:
        data: object = json.loads(text)
    except JSONDecodeError:
        return None

    if isinstance(data, dict):
        return cast("dict[str, object]", data)
    return None


def write_status(path: Path, status: Mapping[str, object]) -> None:
    """Write the status dictionary to the given file as JSON.

    Atomic: temp file then rename, so a reader never sees a half-written file

    Args:
        path (Path): Path to the status file.
        status (Mapping[str, object]): The status dictionary to write to the file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same folder as the target: rename is only atomic within one filesystem
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(status), encoding="utf-8")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _try_lock(handle: BinaryIO) -> None:
    """Take an exclusive, non-blocking lock; raise OSError if it is held.

    Windows: lock one byte at offset 0. Locks there are mandatory, so we never write
    into the locked region.

    Linux: flock attaches the lock to the open file, so a second acquire in the same
    process also fails.

    Args:
        handle (BinaryIO): _description_
    """
    if sys.platform == "win32":
        import msvcrt

        # Windows frees a closed or dead process's locks only after an unspecified delay
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover (Linux: exercised on the Pi)
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    """Release the lock on the given file handle.

    Windows: unlock one byte at offset 0.

    Linux: flock releases the lock on the open file.

    Args:
        handle (BinaryIO): _description_
    """
    if sys.platform == "win32":
        import msvcrt

        # Windows frees a closed or dead process's locks only after an unspecified delay
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def acquire_lock(path: Path) -> Generator[None]:
    """Acquire an exclusive lock on the given path.

    The lock is advisory: it only prevents other instances of this logger from running
    at the same time. It does not prevent other processes from writing to the file.

    Args:
        path (Path): Path to the lock file.

    Raises:
        AlreadyRunningError: If another instance holds the lock.

    Yields:
        Generator[None]: yields control to the caller while the lock is held.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("ab")
    # Only unlock a lock we actually took
    locked = False
    try:
        try:
            _try_lock(handle)
            locked = True
        except OSError as exc:
            raise AlreadyRunningError("another instance holds the lock") from exc
        yield
    # The lock is released when the context exits, even if an exception occurs.
    finally:
        if locked:
            _unlock(handle)
        handle.close()
