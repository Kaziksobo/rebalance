"""Logger file for calling TfL API and collecting live data.

Will run on the Raspberry Pi
"""

import gzip
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote

MASK = "***"
_APP_KEY_VALUE = re.compile(r"(app_key=)[^&\s'\"),]*", re.IGNORECASE)

DEFAULT_INTERVAL_S = 60.0
DEFAULT_REQUEST_TIMEOUT_S = 10.0
DEFAULT_DEADLINE_S = 30.0

_SETTING_NAMES = ("TFL_APP_KEY", "REBALANCE_DATA_DIR")

CLOCK_FLOOR = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
CLOCK_TOLERANCE_S = 60.0

SCHEMA_VERSION = 1


class DeadlineExceededError(Exception):
    """Raised when one request runs past its total deadline."""


class ConfigError(Exception):
    """Raised for bad configuration. Messages must never contain values."""


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
    """

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


def redact(text: str, key: str) -> str:
    """Redact the given key from the text.

    Args:
        text: The text to redact the key from.
        key: The key to redact.

    Returns:
        The text with the key redacted.
    """
    if key:
        for form in (quote(key, safe=""), key):
            text = text.replace(form, MASK)
    return _APP_KEY_VALUE.sub(rf"\1{MASK}", text)


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

    Raises:
        ConfigError: If key is missing or blank, or there is a non-positive interval.
    """
    env_file_path = find_env_file(search_from)
    file_vars: dict[str, str] = {}
    if env_file_path:
        file_text = env_file_path.read_text(encoding="utf-8-sig")
        file_vars = parse_env_file(file_text)

    values = {
        name: environ.get(name) or file_vars.get(name, "") for name in _SETTING_NAMES
    }

    base_dir = env_file_path.parent if env_file_path else search_from
    data_dir = (base_dir / (values["REBALANCE_DATA_DIR"] or "data")).resolve()

    return Settings(
        api_key=values["TFL_APP_KEY"],
        data_dir=data_dir,
        interval_s=interval_s,
    )


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
    if interval_s <= 0:
        raise ValueError("interval must be positive")

    count = math.ceil((now - 0) / interval_s)
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
    if isinstance(exc, OSError):
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
