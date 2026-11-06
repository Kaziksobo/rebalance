import logging
from collections.abc import Callable

import pytest

from rebalance.collect import (
    CLOCK_FLOOR,
    CLOCK_RECHECK_S,
    EXIT_OK,
    EXIT_RUNTIME,
    AlreadyRunningError,
    Settings,
    acquire_lock,
    read_status,
    run,
    write_status,
)

KEY = "TESTKEY123"
BASE = 1_793_000_040.0  # a multiple of 60, as in the schedule tests


class FakeTime:
    """A clock whose wait() moves time forward instead of sleeping."""

    def __init__(self, start: float, jumps: dict[int, float] | None = None) -> None:
        self.now = start
        self.waits: list[float] = []
        self.stop = False  # run() exits cleanly when wait() returns True
        # Maps "after the Nth wait" to a new time, to simulate clock being corrected.
        self.jumps = jumps or {}

    def clock(self) -> float:
        return self.now

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.now += max(seconds, 0.0)
        if len(self.waits) in self.jumps:
            self.now = self.jumps[len(self.waits)]
        return self.stop


class Poller:
    """A fake poll: records when it ran and plays back scripted results.

    A result can be a bool (the poll's outcome) or an exception (raised).
    After the last result, the next wait() ends the loop.
    """

    def __init__(
        self, fake: FakeTime, results: list[bool | Exception], advance: float = 0.0
    ) -> None:
        self.fake = fake
        self.results = results
        self.advance = advance  # how long each poll "takes"
        self.times: list[float] = []

    def __call__(self, settings: Settings) -> bool:
        self.times.append(self.fake.now)
        self.fake.now += self.advance
        result = self.results[len(self.times) - 1]
        if len(self.times) >= len(self.results):
            self.fake.stop = True
        if isinstance(result, Exception):
            raise result
        return result


def test_run_polls_on_the_tick_grid(make_settings: Callable[..., Settings]) -> None:
    fake = FakeTime(BASE + 17.5)
    poller = Poller(fake, [True, True, True])

    code = run(make_settings(), wait=fake.wait, clock=fake.clock, poll=poller)

    assert poller.times == [BASE + 60, BASE + 120, BASE + 180]
    assert code == EXIT_OK


def test_run_stop_request_exits_cleanly(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings()
    fake = FakeTime(BASE + 17.5)
    fake.stop = True  # a stop is already requested

    code = run(settings, wait=fake.wait, clock=fake.clock, poll=Poller(fake, [True]))

    assert code == EXIT_OK
    with acquire_lock(settings.lock_path):  # the lock was released on the way out
        pass


def test_run_slow_poll_skips_missed_ticks(
    make_settings: Callable[..., Settings],
) -> None:
    fake = FakeTime(BASE + 17.5)
    poller = Poller(fake, [True, True], advance=150.0)  # each poll takes 150 s

    run(make_settings(), wait=fake.wait, clock=fake.clock, poll=poller)

    assert poller.times == [BASE + 60, BASE + 240]  # no catch-up polls in between


def test_run_records_outcomes_in_the_status_file(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings()
    fake = FakeTime(BASE + 17.5)
    poller = Poller(fake, [False, False, True])

    run(settings, wait=fake.wait, clock=fake.clock, poll=poller)

    status = read_status(settings.status_path)
    assert status is not None
    assert (status["successes"], status["failures"]) == (1, 2)
    assert status["consecutive_failures"] == 0
    assert status["last_success"] == BASE + 180


def test_run_exception_in_poll_is_a_failed_poll_not_a_crash(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings()
    fake = FakeTime(BASE + 17.5)
    poller = Poller(fake, [RuntimeError("boom"), True])

    code = run(settings, wait=fake.wait, clock=fake.clock, poll=poller)

    status = read_status(settings.status_path)
    assert code == EXIT_OK
    assert len(poller.times) == 2
    assert status is not None
    assert (status["failures"], status["successes"]) == (1, 1)


def test_run_os_error_from_poll_stops_the_loop(
    make_settings: Callable[..., Settings],
) -> None:
    fake = FakeTime(BASE + 17.5)
    poller = Poller(fake, [OSError("disk full"), True, True])  # later results unused

    code = run(make_settings(), wait=fake.wait, clock=fake.clock, poll=poller)

    assert code == EXIT_RUNTIME
    assert len(poller.times) == 1


def test_run_refuses_to_start_when_lock_is_held(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings()
    fake = FakeTime(BASE + 17.5)
    poller = Poller(fake, [True])

    with acquire_lock(settings.lock_path), pytest.raises(AlreadyRunningError):
        run(settings, wait=fake.wait, clock=fake.clock, poll=poller)

    assert poller.times == []


def test_run_waits_for_a_plausible_clock_before_polling(
    make_settings: Callable[..., Settings],
) -> None:
    fake = FakeTime(CLOCK_FLOOR - 1000, jumps={1: BASE + 17.5})
    poller = Poller(fake, [True])

    run(make_settings(), wait=fake.wait, clock=fake.clock, poll=poller)

    assert fake.waits[0] == CLOCK_RECHECK_S
    assert poller.times == [BASE + 60]  # first poll is on the grid after the jump


def test_run_waits_when_clock_is_behind_the_last_recorded_attempt(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings()
    write_status(settings.status_path, {"last_attempt": BASE + 10_000.0})
    fake = FakeTime(BASE + 17.5, jumps={1: BASE + 10_017.5})
    poller = Poller(fake, [True])

    run(settings, wait=fake.wait, clock=fake.clock, poll=poller)

    assert fake.waits[0] == CLOCK_RECHECK_S
    assert poller.times == [BASE + 10_020.0]


def test_run_status_write_failure_is_not_fatal(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_write(path: object, status: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr("rebalance.collect.write_status", failing_write)
    fake = FakeTime(BASE + 17.5)
    poller = Poller(fake, [True, True])

    code = run(make_settings(), wait=fake.wait, clock=fake.clock, poll=poller)

    assert code == EXIT_OK
    assert len(poller.times) == 2


def test_run_never_logs_the_key_or_a_traceback(
    make_settings: Callable[..., Settings], caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeTime(BASE + 17.5)
    poller = Poller(fake, [RuntimeError(f"failed: ?app_key={KEY}")])

    with caplog.at_level(logging.DEBUG):
        run(make_settings(api_key=KEY), wait=fake.wait, clock=fake.clock, poll=poller)

    assert KEY not in caplog.text
    assert "Traceback" not in caplog.text
    assert "RuntimeError" in caplog.text  # the label was logged


def test_run_stop_during_startup_clock_wait_exits_cleanly(
    make_settings: Callable[..., Settings],
) -> None:
    fake = FakeTime(CLOCK_FLOOR - 1000)
    fake.stop = True
    poller = Poller(fake, [True])

    code = run(make_settings(), wait=fake.wait, clock=fake.clock, poll=poller)

    assert code == EXIT_OK
    assert poller.times == []
    assert fake.waits == [CLOCK_RECHECK_S]


def test_run_re_anchors_when_the_clock_steps_backwards_mid_run(
    make_settings: Callable[..., Settings],
) -> None:
    # wait 1 = tick wait before poll 1; wait 2 = tick wait before poll 2 (the clock
    # steps back to before the last attempt); wait 3 = recheck wait (clock recovers).
    fake = FakeTime(BASE + 17.5, jumps={2: BASE - 1000, 3: BASE + 500})
    poller = Poller(fake, [True, True])

    run(make_settings(), wait=fake.wait, clock=fake.clock, poll=poller)

    assert poller.times == [
        BASE + 60,
        BASE + 540,
    ]  # re-anchored to the next whole minute


def test_run_logs_recovery_after_failures(
    make_settings: Callable[..., Settings], caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeTime(BASE + 17.5)
    poller = Poller(fake, [False, False, True])

    with caplog.at_level(logging.INFO):
        run(make_settings(), wait=fake.wait, clock=fake.clock, poll=poller)

    assert "recovered after 2 failed polls" in caplog.text


def test_run_does_not_log_recovery_without_failures(
    make_settings: Callable[..., Settings], caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeTime(BASE + 17.5)
    poller = Poller(fake, [True, True])

    with caplog.at_level(logging.INFO):
        run(make_settings(), wait=fake.wait, clock=fake.clock, poll=poller)

    assert "recovered" not in caplog.text
