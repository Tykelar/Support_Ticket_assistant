"""Time is a dependency, not an ambient fact (ADR 0008).

The advancing tick is the part worth testing: a constant clock would also be
deterministic, but it makes every duration zero and hides a step recorded out of order.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from support_assistant.clock import FrozenClock, SystemClock

# --------------------------------------------------------------------------------------
# SystemClock -- what the running service uses.
# --------------------------------------------------------------------------------------


def test_system_clock_returns_timezone_aware_utc() -> None:
    # A naive datetime written to the trace is ambiguous the moment anyone reads it in
    # another timezone, and STORAGE.md's `ts` column says ISO-8601 UTC.
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_system_clock_does_not_go_backwards() -> None:
    clock = SystemClock()
    first = clock.now()
    second = clock.now()
    assert second >= first


# --------------------------------------------------------------------------------------
# FrozenClock -- what the suite uses.
# --------------------------------------------------------------------------------------


def test_frozen_clock_starts_at_its_start_instant() -> None:
    start = datetime(2026, 8, 31, 10, 14, 2, tzinfo=UTC)
    assert FrozenClock(start=start).now() == start


def test_frozen_clock_advances_exactly_one_tick_per_call() -> None:
    clock = FrozenClock()
    first = clock.now()
    second = clock.now()
    assert second - first == timedelta(milliseconds=10)


def test_frozen_clock_advances_by_arithmetic() -> None:
    # This is what makes pipeline_duration_seconds assertable: a known tick times a known
    # number of steps is arithmetic, not a tolerance window.
    start = datetime(2026, 1, 1, tzinfo=UTC)
    clock = FrozenClock(start=start)
    readings = [clock.now() for _ in range(10)]
    assert readings == [start + timedelta(milliseconds=10 * i) for i in range(10)]


def test_frozen_clock_readings_strictly_increase() -> None:
    # The property that lets test_tracing.py assert `ts` increases with `seq`. A constant
    # clock would make that assertion vacuous.
    clock = FrozenClock()
    readings = [clock.now() for _ in range(5)]
    assert all(b > a for a, b in zip(readings, readings[1:], strict=False))


def test_frozen_clock_honours_a_custom_tick() -> None:
    clock = FrozenClock(start=datetime(2026, 1, 1, tzinfo=UTC), tick=timedelta(seconds=1))
    first = clock.now()
    second = clock.now()
    assert second - first == timedelta(seconds=1)


def test_two_frozen_clocks_produce_the_same_readings() -> None:
    # Byte-for-byte identical traces across runs is the whole reason this exists.
    a, b = FrozenClock(), FrozenClock()
    assert [a.now() for _ in range(5)] == [b.now() for _ in range(5)]


def test_frozen_clock_returns_timezone_aware_utc() -> None:
    assert FrozenClock().now().utcoffset() == timedelta(0)


# --------------------------------------------------------------------------------------
# The guard -- ADR 0008 enforced mechanically rather than by discipline.
# --------------------------------------------------------------------------------------

SRC = Path(__file__).resolve().parent.parent / "src" / "support_assistant"
AMBIENT_TIME = ("datetime.now(", "datetime.utcnow(", ".utcnow(")


def test_nothing_outside_clock_module_reads_the_wall_clock() -> None:
    """ADR 0008: nothing in the system calls datetime.now() directly.

    Threading a clock through afterwards touches every component that records anything,
    which is why this is a test and not a code review note.
    """
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if path.name != "clock.py"
        and any(marker in path.read_text(encoding="utf-8") for marker in AMBIENT_TIME)
    ]
    assert offenders == [], (
        f"{offenders} read time ambiently; take a Clock as a dependency instead"
    )


def test_the_guard_can_actually_fail() -> None:
    # A guard that cannot fail is decoration. This proves the marker matching works
    # against the line it is meant to catch.
    assert any(marker in "ts = datetime.now(UTC)" for marker in AMBIENT_TIME)
