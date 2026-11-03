from collections.abc import Callable
from pathlib import Path

import pytest

from rebalance.collect import (
    ConfigError,
    Settings,
    find_env_file,
    load_settings,
    parse_env_file,
)

KEY = "TESTKEY123"

WriteEnv = Callable[..., Path]


# parse_env_file tests
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A=1\n", {"A": "1"}),
        ("# comment\n\nA=1\n", {"A": "1"}),
        ("A=b=c\n", {"A": "b=c"}),
        ('A="quoted"\n', {"A": "quoted"}),
        ("A=1\r\nB=2\r\n", {"A": "1", "B": "2"}),
        ("  A  =  value  \n", {"A": "value"}),
        ("A=value # this is a comment\n", {"A": "value # this is a comment"}),
        ("A=\n", {"A": ""}),
        ("A=value#notacomment\n", {"A": "value#notacomment"}),
    ],
)
def test_parse_env_file_handles(text: str, expected: dict[str, str]) -> None:
    assert parse_env_file(text) == expected


def test_parse_env_file_error_does_not_echo_line() -> None:
    with pytest.raises(ConfigError) as excinfo:
        parse_env_file("SECRETVALUEWITHOUTNAME\n")

    assert "SECRETVALUE" not in str(excinfo.value)


def test_parse_env_file_prefers_last_occurrence_of_key() -> None:
    text = "A=1\nA=2\n"
    expected = {"A": "2"}
    assert parse_env_file(text) == expected


def test_parse_env_file_error_has_line_number() -> None:
    with pytest.raises(ConfigError) as excinfo:
        parse_env_file("A=1\nB\nC=3\n")

    assert "line 2" in str(excinfo.value)


# find_env_file tests
def test_find_env_file_in_start_folder(tmp_path: Path, write_env: WriteEnv) -> None:
    env = write_env("A=1\n")

    assert find_env_file(tmp_path) == env.resolve()


def test_find_env_file_respects_level_limit(
    tmp_path: Path, write_env: WriteEnv
) -> None:
    write_env("A=1\n")
    deep = tmp_path / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)

    assert find_env_file(deep, max_levels=4) is None
    assert find_env_file(deep, max_levels=5) == (tmp_path / ".env").resolve()


def test_find_env_file_returns_none_if_not_found(tmp_path: Path) -> None:
    assert find_env_file(tmp_path) is None


def test_find_env_file_finds_in_parent(tmp_path: Path, write_env: WriteEnv) -> None:
    write_env("A=1\n")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)

    assert find_env_file(deep) == (tmp_path / ".env").resolve()


# load_settings tests
def test_load_settings_reads_environment_when_no_env_file(tmp_path: Path) -> None:
    settings = load_settings(tmp_path, {"TFL_APP_KEY": KEY})

    assert settings.api_key == KEY


def test_load_settings_environment_beats_file(
    tmp_path: Path, write_env: WriteEnv
) -> None:
    write_env("TFL_APP_KEY=filekey\n")

    settings = load_settings(tmp_path, {"TFL_APP_KEY": KEY})

    assert settings.api_key == KEY


def test_load_settings_handles_key_only_in_file(
    tmp_path: Path, write_env: WriteEnv
) -> None:
    write_env(f"TFL_APP_KEY={KEY}\n")

    settings = load_settings(tmp_path, {})

    assert settings.api_key == KEY


def test_load_settings_falls_back_to_file_when_env_val_empty(
    tmp_path: Path, write_env: WriteEnv
) -> None:
    write_env(f"TFL_APP_KEY={KEY}\n")

    settings = load_settings(tmp_path, {"TFL_APP_KEY": ""})

    assert settings.api_key == KEY


def test_load_settings_environment_beats_file_for_data_dir(
    tmp_path: Path, write_env: WriteEnv
) -> None:
    write_env(f"REBALANCE_DATA_DIR={tmp_path / 'file'}\n")
    search_from = tmp_path / "nested"
    search_from.mkdir()
    env_data_dir = tmp_path / "env"

    settings = load_settings(
        search_from,
        {"TFL_APP_KEY": KEY, "REBALANCE_DATA_DIR": str(env_data_dir)},
    )

    assert settings.data_dir == env_data_dir.resolve()


def test_load_settings_handles_missing_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(tmp_path, {})


def test_load_settings_handles_blank_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(tmp_path, {"TFL_APP_KEY": ""})


def test_load_settings_handles_relative_data_dir(tmp_path: Path) -> None:
    settings = load_settings(
        tmp_path, {"TFL_APP_KEY": KEY, "REBALANCE_DATA_DIR": "rel"}
    )

    assert settings.data_dir == (tmp_path / "rel").resolve()


def test_load_settings_resolves_relative_data_dir_against_env_file_folder(
    tmp_path: Path, write_env: WriteEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_env(f"TFL_APP_KEY={KEY}\nREBALANCE_DATA_DIR=mydata\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.chdir(tmp_path.parent)

    settings = load_settings(sub, {})

    assert settings.data_dir == (tmp_path / "mydata").resolve()


def test_load_settings_handles_absolute_data_dir(tmp_path: Path) -> None:
    absolute_data_dir = tmp_path / "elsewhere"
    search_from = tmp_path / "search"
    search_from.mkdir()

    settings = load_settings(
        search_from,
        {"TFL_APP_KEY": KEY, "REBALANCE_DATA_DIR": str(absolute_data_dir)},
    )

    assert settings.data_dir == absolute_data_dir.resolve()


def test_load_settings_handles_missing_data_dir(tmp_path: Path) -> None:
    settings = load_settings(tmp_path, {"TFL_APP_KEY": KEY})

    assert settings.data_dir == (tmp_path / "data").resolve()


def test_load_settings_non_positive_interval_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(tmp_path, {"TFL_APP_KEY": KEY}, interval_s=0)


def test_load_settings_handles_file_with_bom(tmp_path: Path) -> None:
    bom = b"\xef\xbb\xbf"
    content = bom + b"TFL_APP_KEY=keywithbom\n"
    path = tmp_path / ".env"
    path.write_bytes(content)

    settings = load_settings(tmp_path, {})

    assert settings.api_key == "keywithbom"


def test_load_settings_handles_file_with_unrelated_vars(
    tmp_path: Path, write_env: WriteEnv
) -> None:
    write_env("UNRELATED_VAR=foo\nTFL_APP_KEY=key\nANOTHER_VAR=bar\n")

    settings = load_settings(tmp_path, {})

    assert settings.api_key == "key"


def test_load_settings_error_names_variable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_settings(tmp_path, {})

    assert "TFL_APP_KEY" in str(excinfo.value)


# Settings tests
def test_settings_repr_does_not_contain_key(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(api_key=KEY)

    assert KEY not in repr(settings)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"interval_s": 0}, id="zero_interval"),
        pytest.param({"interval_s": -1}, id="negative_interval"),
        pytest.param({"request_timeout_s": 0}, id="zero_request_timeout"),
        pytest.param(
            {"request_timeout_s": 31, "deadline_s": 30}, id="request_above_deadline"
        ),
    ],
)
def test_settings_rejects_bad_numbers(
    make_settings: Callable[..., Settings], overrides: dict[str, float]
) -> None:
    with pytest.raises(ConfigError):
        make_settings(**overrides)


def test_settings_derived_paths_hang_off_data_dir(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings()

    assert settings.live_dir == settings.data_dir / "live"
    assert settings.lock_path == settings.live_dir / ".logger.lock"
    assert settings.status_path == settings.live_dir / ".status.json"
    assert settings.log_path == settings.live_dir / "logger.log"
