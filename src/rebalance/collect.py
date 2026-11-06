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

Usage:
    python3 collect.py [--once] [--interval SECONDS]

Settings come from the environment or a .env file: TFL_APP_KEY (required) and
REBALANCE_DATA_DIR (default: ``data``, beside the .env). Output goes to
<data dir>/live/: the day files (YYYY-MM-DD.jsonl.gz), .status.json, .logger.lock
and logger.log.

Exit codes: 0 clean stop, 1 runtime failure, 2 configuration error,
3 another instance is already running.
"""

import argparse
import gzip
import json
import logging
import math
import os
import re
import signal
import sys
import threading
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.client import HTTPException, IncompleteRead
from json import JSONDecodeError
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import BinaryIO, TextIO, TypedDict, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

# --- Constants and exceptions ---
MASK = "***"
# Masks the value of any app_key parameter, even one that isn't the key we hold,
# so a stale or wrong key can't leak either. The value ends at &, whitespace,
# a quote, a bracket or a comma.
_APP_KEY_VALUE = re.compile(r"(app_key=)[^&\s'\"),]*", re.IGNORECASE)


DEFAULT_INTERVAL_S = 60.0
# The per-operation timeout bounds each blocking socket call; the deadline bounds
# the whole request. urllib's timeout alone doesn't cap total time, so both are needed.
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


API_URL = "https://api.tfl.gov.uk/BikePoint"
# Clock recheck interval: if the clock is implausible, wait this long and check again.
CLOCK_RECHECK_S = 30.0
# Exit codes: 0 clean stop, 1 runtime failure, 2 configuration error,
# 3 another instance is already running.
EXIT_OK, EXIT_RUNTIME, EXIT_CONFIG, EXIT_RUNNING = 0, 1, 2, 3


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
        start (Path): The starting directory to search from.
        max_levels (int): The maximum number of parent directories to search.

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
    """Resolve the settings.

    The environment beats the .env file, and an empty environment value counts as unset.
    A relative REBALANCE_DATA_DIR is anchored to the .env's folder
    (or search_from if there's no file), never the working directory.

    Args:
        search_from (Path): The directory to start searching for a .env file.
        environ (Mapping[str, str]): The environment variables to use.
        interval_s (float, optional): The interval between polls.
            Defaults to DEFAULT_INTERVAL_S.

    Returns:
        Settings: The loaded settings.

    Raises:
        ConfigError: If the key is missing, the interval is not positive, or the
            .env file has an invalid line.
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
        text (str): The text of the .env file.

    Returns:
        dict[str, str]: A dictionary of key-value pairs.

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
        text (str): The text to redact the key from.
        key (str): The key to redact.

    Returns:
        str: The text with the key redacted.
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
        now (float): Current time, epoch seconds.
        interval_s (float): Seconds between ticks.

    Returns:
        float: Absolute epoch time of the first grid point at or after ``now``.

    Raises:
        ValueError: If ``interval_s`` is not positive.
    """
    if interval_s <= 0:
        raise ValueError("interval must be positive")

    # The grid is anchored at the epoch, so a 60 s interval ticks on whole minutes.
    count = math.ceil(now / interval_s)
    return count * interval_s


def next_tick(previous: float, interval_s: float, now: float) -> float:
    """Return the first scheduled tick strictly after ``now``.

    Ticks lie on ``previous + k * interval_s``. Missed ticks are skipped,
    never returned, so a long sleep produces one tick, not a burst.

    Args:
        previous (float): The last tick time, epoch seconds.
        interval_s (float): Seconds between ticks.
        now (float): Current time, epoch seconds.

    Returns:
        float: Absolute epoch time of the first grid point strictly after now.

    Raises:
        ValueError: If ``interval_s`` is not positive.
    """
    if interval_s <= 0:
        raise ValueError("interval must be positive")
    elapsed = now - previous
    # max(..., 1) stops a clock that went backwards returning an earlier tick.
    # The +1 makes the result strictly after now.
    k = max(math.floor(elapsed / interval_s) + 1, 1)
    result = previous + k * interval_s
    # Guard against float rounding putting the result at or before now.
    if result <= now:
        result += interval_s
    return result


def clock_plausible(now: float, last_record: float | None) -> bool:
    """Return True if the system clock looks sane.

    The clock must not be before CLOCK_FLOOR, and must not be earlier than
    the last record by more than CLOCK_TOLERANCE_S. A clock far *ahead* of the
    last record is normal after an outage and is accepted.

    Args:
        now (float): Current time, epoch seconds.
        last_record (float | None): Timestamp of the last record, or None if no records.

    Returns:
        bool: True if the clock is plausible, False otherwise.
    """
    if now < CLOCK_FLOOR:
        return False
    return not (last_record is not None and now < last_record - CLOCK_TOLERANCE_S)


# --- Records and encoding ---
def error_class(exc: BaseException) -> str:
    """Map an exception to a label for logging and metrics.

    Return http_<code>, deadline, timeout, network, or the exception's type name.

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
    """Check that ``body`` has the shape of a JSON array.

    A cheap check only: it doesn't parse, because parsing a 2 MB body each
    minute is slow on the Pi. BikePoint returns an array, so a JSON error
    object (which starts with ``{``) counts as not OK.

    Args:
        body (str): The response text.

    Returns:
        bool: True if the stripped text starts with ``[`` and ends with ``]``.
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

    The payload is the raw response text, never parsed: parsing 2 MB each minute is
    slow on the Pi, and raw text is guaranteed untouched.
    payload_ok is only a cheap shape check.

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

    Each record is one JSON line compressed as its own gzip member. Members concatenate
    into a valid gzip stream, so appending is safe and a crash costs at most the last
    poll. mtime=0 makes the output deterministic.

    Args:
        record (Mapping[str, object]): The record to encode.

    Returns:
        bytes: The gzip-compressed JSON line.
    """
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
    """Fetch url. Expected failures are returned as an error label, never raised.

    Args:
        url (str): Full URL, including any key. Never logged or returned.
        timeout_s (float): Timeout for each blocking socket operation.
        deadline_s (float): Total time allowed, checked on a monotonic clock.
        monotonic (Callable[[], float]): Clock function, injectable for tests.

    Returns:
        FetchResult: The outcome, any failure is in error.
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
    """Append one gzip member to ``path``, durably.

    One call writes one whole member, so a crash leaves at most one partial
    member at the end. The data is flushed and fsynced before returning: the Pi
    will see power cuts, and a day's data can't be recreated.

    Args:
        path (Path): The day file. Missing parent folders are created.
        data (bytes): One complete gzip member, as produced by ``encode_member``.

    Raises:
        OSError: If the write fails, for example when the disk is full.
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

    Returns a new dict; missing or damaged values count as zero,
    so a corrupt status file can't stop the logger at start-up.

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
    """Read the status file, or return None if it is unusable.

    The file is advisory, so any damage (missing, unreadable, not JSON, or not a
    JSON object) means "no status" and never stops the logger.

    Args:
        path (Path): Path to the status file.

    Returns:
        dict[str, object] | None: The parsed status object, or None.
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

    Atomic: temp file then rename, so a reader never sees a half-written file.

    Args:
        path (Path): Path to the status file.
        status (Mapping[str, object]): The status dictionary to write to the file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same folder as the target: rename is only atomic within one filesystem.
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
        handle (BinaryIO): The file handle to lock.
    """
    if sys.platform == "win32":
        import msvcrt

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
        handle (BinaryIO): The file handle to unlock.
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
    """Hold an exclusive lock on ``path`` for the duration of the block.

    The lock is released when the block exits, including after an error, and
    by the OS if the process dies. The file itself holds no data.

    Args:
        path (Path): The lock file. Missing parent folders are created.

    Raises:
        AlreadyRunningError: If another instance already holds the lock.

    Yields:
        None: Control returns to the caller while the lock is held.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("ab")
    # Only unlock a lock we actually took.
    locked = False
    try:
        try:
            _try_lock(handle)
            locked = True
        except OSError as exc:
            raise AlreadyRunningError("another instance holds the lock") from exc
        yield
    finally:
        # The lock is released when the context exits, even if an exception occurs.
        if locked:
            _unlock(handle)
        handle.close()


# --- Logging, poll loop and entry point ---
class RedactingFormatter(logging.Formatter):
    """Format log lines in UTC, passing the finished text through ``redact``.

    Redacting after formatting covers everything: the message, its arguments
    and any traceback text.
    """

    converter = staticmethod(time.gmtime)

    def __init__(self, key: str) -> None:
        """Initialize the formatter with a redaction key."""
        super().__init__("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%SZ")
        self._key = key

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record, redacting the key from the output."""
        return redact(super().format(record), self._key)


def configure_logging(
    settings: Settings, *, stream: TextIO | None = None
) -> logging.Logger:
    """Send the logger's messages to a rotating file and to a stream.

    Lines are timestamped in UTC and pass through ``redact``, so neither the
    key nor an exception's text can leak. The file rotates at about 1 MB, keeping
    three backups. Existing handlers are removed first, so calling this twice
    doesn't duplicate lines.

    Args:
        settings (Settings): Supplies the log file path and the key to redact.
        stream (TextIO | None, optional): Where to also write. Defaults to stderr.

    Returns:
        logging.Logger: The configured rebalance.collect logger.
    """
    settings.live_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("rebalance.collect")
    # Clear old handlers so a second call doesn't duplicate every line.
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    logger.setLevel(logging.INFO)

    formatter = RedactingFormatter(settings.api_key)
    file_handler = RotatingFileHandler(
        settings.log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    stream_handler = logging.StreamHandler(stream)  # None means stderr
    for handler in (file_handler, stream_handler):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


# Polling function type: takes Settings, returns True on success, False on failure.
PollFn = Callable[[Settings], bool]


def poll_once(
    settings: Settings,
    *,
    now: Callable[[], float] = time.time,
    fetch_fn: Callable[..., FetchResult] = fetch,
    append: Callable[[Path, bytes], None] = append_member,
    log: logging.Logger | None = None,
) -> bool:
    """Poll the API once and always append a record, success or failure.

    A failed request still produces a record (with an error label), so gaps in
    the data are explicit. The record is filed by when the request started.

    Args:
        settings (Settings): The settings object containing configuration for the poll.
        now (Callable[[], float], optional): Clock function. Defaults to time.time.
        fetch_fn (Callable[..., FetchResult], optional): Data fetch function.
            Defaults to fetch.
        append (Callable[[Path, bytes], None], optional): Data append function.
            Defaults to append_member.
        log (logging.Logger | None, optional): Logger for warnings.
            Defaults to the module's logger.

    Returns:
        bool: True if the request succeeded and the payload looks like a JSON
            array; False for a failed request or a payload of the wrong shape.

    Raises:
        OSError: If the record cannot be written (for example a full disk).
    """
    # The record is filed by when the request STARTED, so read the clock first.
    fetched_at = now()
    # The only place a URL is built. It must never be logged or stored.
    url = f"{API_URL}?app_key={settings.api_key}"
    result = fetch_fn(
        url, timeout_s=settings.request_timeout_s, deadline_s=settings.deadline_s
    )
    record = build_record(
        fetched_at, result.status, result.elapsed_ms, result.error, result.body
    )
    append(day_path(settings.live_dir, fetched_at), encode_member(record))
    ok = result.error is None and bool(record["payload_ok"])
    if not ok:
        log = log or logging.getLogger("rebalance.collect")
        log.warning(
            "poll failed: %s (status %s)", result.error or "bad_payload", result.status
        )
    return ok


def _wait_for_plausible_clock(
    clock: Callable[[], float],
    wait: Callable[[float], bool],
    last_attempt: float | None,
    log: logging.Logger,
) -> bool:
    """Wait until the clock looks sane. Return True if a stop was requested."""
    while not clock_plausible(clock(), last_attempt):
        log.warning("system clock not plausible; waiting %.0f s", CLOCK_RECHECK_S)
        if wait(CLOCK_RECHECK_S):
            return True
    return False


def run(
    settings: Settings,
    *,
    wait: Callable[[float], bool],
    clock: Callable[[], float] = time.time,
    poll: PollFn = poll_once,
    log: logging.Logger | None = None,
) -> int:
    """Run the collection loop until a stop is requested.

    Polls on the interval grid, records each outcome in the status file, and
    waits for a plausible clock before starting. A failed poll never ends the
    loop; only a stop request, a held lock or a failing disk do.

    Args:
        settings (Settings): The settings object containing configuration for
            the logger.
        wait (Callable[[float], bool]): A function that takes a number of seconds to
            wait and returns True if a stop was requested.
        clock (Callable[[], float], optional): The function to use to get the current
            time. Defaults to time.time.
        poll (PollFn, optional): The function to use for polling the API.
            Defaults to poll_once.
        log (logging.Logger | None, optional): The logger to use. Defaults to the
            modules logger.

    Returns:
        int: Exit code for the run:
            EXIT_OK (0) for a clean stop (a stop was requested),
            EXIT_RUNTIME (1) if the day file could not be written.

    Raises:
        AlreadyRunningError: If another instance holds the lock. ``main`` turns
            this into EXIT_RUNNING.
    """
    log = log or logging.getLogger("rebalance.collect")
    with acquire_lock(settings.lock_path):
        status: Mapping[str, object] = read_status(settings.status_path) or {}
        last_attempt = _timestamp(status.get("last_attempt"))

        if _wait_for_plausible_clock(clock, wait, last_attempt, log):
            return EXIT_OK
        tick = first_tick(clock(), settings.interval_s)

        while True:
            if wait(tick - clock()):  # sleeps until the tick; True means stop
                return EXIT_OK
            now = clock()
            if not clock_plausible(now, last_attempt):
                # The clock moved backwards (e.g. an NTP correction): re-anchor.
                if _wait_for_plausible_clock(clock, wait, last_attempt, log):
                    return EXIT_OK
                tick = first_tick(clock(), settings.interval_s)
                continue

            try:
                ok = poll(settings)
            except OSError as exc:
                # The only OSError source is the day-file write: a full or failing disk.
                # Stop loudly; systemd restarts us.
                log.error("fatal: %s writing the day file", type(exc).__name__)
                return EXIT_RUNTIME
            except Exception as exc:
                # Last resort so one bad poll can't end the loop. Log only the label:
                # str(exc) and tracebacks can contain the URL.
                log.warning("poll failed: %s", error_class(exc))
                ok = False

            failed_before = _count(status.get("consecutive_failures"))
            status = update_status(status, ok, now)
            if ok and failed_before:
                log.info("recovered after %d failed polls", failed_before)
            last_attempt = now
            try:
                write_status(settings.status_path, status)
            except OSError as exc:
                log.warning("could not write status: %s", type(exc).__name__)
            # Skips ticks missed during a slow poll, so there is no catch-up burst.
            tick = next_tick(tick, settings.interval_s, clock())


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] = os.environ,
    search_from: Path | None = None,
    runner: Callable[..., int] = run,
    poll: PollFn = poll_once,
) -> int:
    """Run the logger from the command line.

    Parses the flags, loads the settings, sets up logging, installs handlers so
    SIGINT and SIGTERM request a clean stop, then either polls once (``--once``)
    or runs the loop. The previous signal handlers are restored on the way out.

    The last four arguments are injection points for tests; production calls
    ``main()`` with none of them.

    Args:
        argv (Sequence[str] | None, optional): Command-line arguments, without
            the program name. Defaults to ``sys.argv[1:]``.
        environ (Mapping[str, str], optional): Environment variables to read
            settings from. Defaults to ``os.environ``.
        search_from (Path | None, optional): Folder to start searching upwards
            for a ``.env`` file. Defaults to this file's folder.
        runner (Callable[..., int], optional): The loop to run. Defaults to ``run``.
        poll (PollFn, optional): The single-poll function, used by ``--once`` and
            passed to the runner. Defaults to ``poll_once``.

    Returns:
        int: The process exit code:
            EXIT_OK (0) for a clean stop, or a successful ``--once`` poll,
            EXIT_RUNTIME (1) for a failed ``--once`` poll or a failing disk,
            EXIT_CONFIG (2) for a configuration error,
            EXIT_RUNNING (3) if another instance is already running.

    Raises:
        SystemExit: If a flag can't be parsed (``argparse`` exits with status 2).
    """
    parser = argparse.ArgumentParser(description="TfL BikePoint live-feed logger")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_S,
        help="seconds between polls",
    )
    args = parser.parse_args(argv)  # bad input exits with status 2 by itself

    try:
        settings = load_settings(
            search_from or Path(__file__).resolve().parent,
            environ,
            interval_s=args.interval,
        )
    except ConfigError as exc:
        sys.stderr.write(f"configuration error: {exc}\n")  # messages hold no values
        return EXIT_CONFIG

    log = configure_logging(settings)
    stop = threading.Event()

    def request_stop(signum: int, frame: object) -> None:
        """Handlers must be quick: just set the flag and let the loop shut down."""
        stop.set()

    previous = {
        sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        log.info("starting")
        if args.once:
            # So a manual poll can't race the running service.
            with acquire_lock(settings.lock_path):
                ok = poll(settings)
                log.info("single poll: %s", "ok" if ok else "failed")
                return EXIT_OK if ok else EXIT_RUNTIME
        code = runner(settings, wait=stop.wait, poll=poll)
        log.info("stopped with exit code %d", code)
        return code
    except AlreadyRunningError as exc:
        log.error("%s", exc)  # fixed text, no values
        return EXIT_RUNNING
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
