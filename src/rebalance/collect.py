"""Logger file for calling TfL API and collecting live data.

Will run on the Raspberry Pi
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

MASK = "***"
_APP_KEY_VALUE = re.compile(r"(app_key=)[^&\s'\"),]*", re.IGNORECASE)

DEFAULT_INTERVAL_S = 60.0
DEFAULT_REQUEST_TIMEOUT_S = 10.0
DEFAULT_DEADLINE_S = 30.0

_SETTING_NAMES = ("TFL_APP_KEY", "REBALANCE_DATA_DIR")


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
