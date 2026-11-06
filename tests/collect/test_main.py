import logging
import signal
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from rebalance.collect import (
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_RUNNING,
    EXIT_RUNTIME,
    AlreadyRunningError,
    PollFn,
    Settings,
    acquire_lock,
    main,
)

KEY = "TESTKEY123"


@pytest.fixture(autouse=True)
def close_logging_handlers() -> Iterator[None]:
    yield
    logger = logging.getLogger("rebalance.collect")
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"TFL_APP_KEY": KEY, "REBALANCE_DATA_DIR": str(tmp_path / "data"), **extra}


class FakeRunner:
    """Stands in for run(): records the settings it was given and returns a code."""

    def __init__(self, code: int = EXIT_OK, raises: Exception | None = None) -> None:
        self.code = code
        self.raises = raises
        self.settings: list[Settings] = []

    def __call__(self, settings: Settings, **kwargs: object) -> int:
        self.settings.append(settings)
        if self.raises is not None:
            raise self.raises
        return self.code


class FakePoll:
    """Stands in for poll_once(): records calls and returns a scripted result."""

    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.settings: list[Settings] = []

    def __call__(self, settings: Settings) -> bool:
        self.settings.append(settings)
        return self.result


def test_main_missing_key_returns_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = {"REBALANCE_DATA_DIR": str(tmp_path / "data")}

    code = main(
        [],
        environ=environment,
        search_from=tmp_path,
        runner=FakeRunner(),
        poll=FakePoll(),
    )

    assert code == EXIT_CONFIG
    assert "TFL_APP_KEY" in capsys.readouterr().err


@pytest.mark.parametrize("interval", ["0", "-5"])
def test_main_non_positive_interval_returns_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], interval: str
) -> None:
    code = main(
        ["--interval", interval],
        environ=env(tmp_path),
        search_from=tmp_path,
        runner=FakeRunner(),
        poll=FakePoll(),
    )

    captured = capsys.readouterr()
    assert code == EXIT_CONFIG
    assert KEY not in captured.out + captured.err


def test_main_non_numeric_interval_is_rejected_by_argparse(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(
            ["--interval", "abc"],
            environ=env(tmp_path),
            search_from=tmp_path,
            runner=FakeRunner(),
            poll=FakePoll(),
        )

    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    ("interval_args", "expected"), [([], 60.0), (["--interval", "5"], 5.0)]
)
def test_main_passes_interval_to_runner(
    tmp_path: Path, interval_args: list[str], expected: float
) -> None:
    runner = FakeRunner()

    main(
        interval_args,
        environ=env(tmp_path),
        search_from=tmp_path,
        runner=runner,
        poll=FakePoll(),
    )

    assert [s.interval_s for s in runner.settings] == [expected]


def test_main_returns_the_runners_exit_code(tmp_path: Path) -> None:
    code = main(
        [],
        environ=env(tmp_path),
        search_from=tmp_path,
        runner=FakeRunner(code=EXIT_RUNTIME),
        poll=FakePoll(),
    )

    assert code == EXIT_RUNTIME


def test_main_already_running_from_runner_returns_running_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner(raises=AlreadyRunningError("another instance holds the lock"))

    code = main(
        [], environ=env(tmp_path), search_from=tmp_path, runner=runner, poll=FakePoll()
    )

    assert code == EXIT_RUNNING
    assert "another instance" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("result", "expected"), [(True, EXIT_OK), (False, EXIT_RUNTIME)]
)
def test_main_once_polls_once_and_never_runs_the_loop(
    tmp_path: Path, result: bool, expected: int
) -> None:
    runner, poll = FakeRunner(), FakePoll(result)

    code = main(
        ["--once"],
        environ=env(tmp_path),
        search_from=tmp_path,
        runner=runner,
        poll=poll,
    )

    assert code == expected
    assert len(poll.settings) == 1
    assert runner.settings == []


def test_main_once_refuses_when_lock_is_held(
    tmp_path: Path, make_settings: Callable[..., Settings]
) -> None:
    lock_path = make_settings().lock_path  # same data dir as env(tmp_path)
    poll = FakePoll()

    with acquire_lock(lock_path):
        code = main(
            ["--once"],
            environ=env(tmp_path),
            search_from=tmp_path,
            runner=FakeRunner(),
            poll=poll,
        )

    assert code == EXIT_RUNNING
    assert poll.settings == []


def test_main_once_releases_the_lock_afterwards(
    tmp_path: Path, make_settings: Callable[..., Settings]
) -> None:
    main(
        ["--once"],
        environ=env(tmp_path),
        search_from=tmp_path,
        runner=FakeRunner(),
        poll=FakePoll(),
    )

    with acquire_lock(make_settings().lock_path):
        pass


def test_main_signal_handler_requests_stop(tmp_path: Path) -> None:
    results: list[bool] = []

    def runner(
        settings: Settings, *, wait: Callable[[float], bool], poll: PollFn
    ) -> int:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        results.append(wait(0.0))
        return EXIT_OK

    main(
        [], environ=env(tmp_path), search_from=tmp_path, runner=runner, poll=FakePoll()
    )

    assert results == [True]


@pytest.mark.parametrize("code", [EXIT_OK, EXIT_RUNTIME])
def test_main_logs_the_runners_exit_code_when_the_loop_stops(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], code: int
) -> None:
    main(
        [],
        environ=env(tmp_path),
        search_from=tmp_path,
        runner=FakeRunner(code=code),
        poll=FakePoll(),
    )

    assert f"stopped with exit code {code}" in capsys.readouterr().err


def test_main_once_does_not_log_a_stopped_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(
        ["--once"],
        environ=env(tmp_path),
        search_from=tmp_path,
        runner=FakeRunner(),
        poll=FakePoll(),
    )

    assert "stopped with exit code" not in capsys.readouterr().err


@pytest.mark.parametrize(("result", "word"), [(True, "ok"), (False, "failed")])
def test_main_once_logs_the_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], result: bool, word: str
) -> None:
    main(
        ["--once"],
        environ=env(tmp_path),
        search_from=tmp_path,
        runner=FakeRunner(),
        poll=FakePoll(result),
    )

    assert f"single poll: {word}" in capsys.readouterr().err
