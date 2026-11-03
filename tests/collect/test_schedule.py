import pytest

from rebalance.collect import (
    CLOCK_FLOOR,
    clock_plausible,
    first_tick,
    next_tick,
)

# first_tick tests
BASE = 1_793_000_040.0  # multiple of 60 (and so of 30 and 10): 2026-10-26 07:34:00 UTC


@pytest.mark.parametrize(
    ("now", "interval_s", "expected"),
    [
        pytest.param(BASE, 60.0, BASE, id="on_boundary"),
        pytest.param(BASE + 1, 60.0, BASE + 60, id="just_after_boundary"),
        pytest.param(BASE + 59.9, 60.0, BASE + 60, id="just_before_boundary"),
        pytest.param(BASE + 1, 10.0, BASE + 10, id="interval_10"),
        pytest.param(BASE + 1, 30.0, BASE + 30, id="interval_30"),
        pytest.param(BASE + 1, 60.0, BASE + 60, id="interval_60"),
    ],
)
def test_first_tick(now: float, interval_s: float, expected: float) -> None:
    result = first_tick(now, interval_s)

    assert result == expected
    assert result >= now
    assert result % interval_s == 0


# next_tick tests
@pytest.mark.parametrize(
    ("previous", "interval_s", "now", "expected"),
    [
        pytest.param(
            1_793_000_000.0, 60.0, 1_793_000_030.0, 1_793_000_060.0, id="on_time"
        ),
        pytest.param(
            1_793_000_000.0,
            60.0,
            1_793_000_060.0,
            1_793_000_120.0,
            id="exactly_on_time",
        ),
        pytest.param(
            1_793_000_000.0, 60.0, 1_793_000_061.0, 1_793_000_120.0, id="one_overrun"
        ),
        pytest.param(
            1_793_000_000.0,
            60.0,
            1_793_001_800.0,
            1_793_001_860.0,
            id="long_sleep",
        ),
    ],
)
def test_next_tick(
    previous: float, interval_s: float, now: float, expected: float
) -> None:
    result = next_tick(previous, interval_s, now)
    assert result == expected
    assert (result - previous) % interval_s == 0
    assert result > now
    assert result - now <= interval_s


def test_next_tick_when_clock_went_backwards() -> None:
    previous = 1_793_000_000.0

    result = next_tick(previous, 60.0, previous - 100)

    assert result == previous + 60


@pytest.mark.parametrize("interval_s", [0.0, -1.0])
def test_first_tick_rejects_non_positive_interval(interval_s: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        first_tick(BASE, interval_s)


@pytest.mark.parametrize("interval_s", [0.0, -1.0])
def test_next_tick_rejects_non_positive_interval(interval_s: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        next_tick(BASE, interval_s, BASE)


# clock_plausible tests
@pytest.mark.parametrize(
    ("now", "last_record", "expected"),
    [
        pytest.param(1_800_000_000.0, None, True, id="no_last_record"),
        pytest.param(
            1_800_000_000.0, 1_799_999_000.0, True, id="last_record_before_now"
        ),
        pytest.param(
            1_800_000_000.0, 1_800_000_000.0, True, id="last_record_equal_now"
        ),
        pytest.param(
            1_800_000_000.0,
            1_800_000_030.0,
            True,
            id="last_record_after_now_within_tolerance",
        ),
        pytest.param(
            1_800_000_000.0,
            1_800_000_060.0,
            True,
            id="last_record_after_now_at_tolerance",
        ),
        pytest.param(
            1_800_000_000.0,
            1_800_000_061.0,
            False,
            id="last_record_after_now_beyond_tolerance",
        ),
        pytest.param(CLOCK_FLOOR, None, True, id="now_at_floor"),
        pytest.param(
            1_800_000_000.0 + 30 * 24 * 60 * 60,
            1_800_000_000.0,
            True,
            id="month_after_last_record",
        ),
        pytest.param(CLOCK_FLOOR - 1, None, False, id="just_before_floor"),
    ],
)
def test_clock_plausible(now: float, last_record: float | None, expected: bool) -> None:
    assert clock_plausible(now, last_record) == expected
