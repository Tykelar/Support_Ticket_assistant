"""Time as a dependency, not an ambient fact (ADR 0008).

Nothing in this system calls `datetime.now()` directly -- a test in tests/test_clock.py
greps for it and fails if it reappears. Everything that records a timestamp takes a
`Clock`.
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol

DEFAULT_START = datetime(2026, 1, 1, tzinfo=UTC)
"""Where FrozenClock begins. Arbitrary, but fixed, so traces are identical run to run."""

DEFAULT_TICK = timedelta(milliseconds=10)
"""How far FrozenClock advances per reading. Small enough to look like real step timing,
large enough to read in a trace."""


class Clock(Protocol):
    """The source of every timestamp in the system."""

    def now(self) -> datetime:
        """The current instant, timezone-aware and in UTC."""
        ...


class SystemClock:
    """Real wall-clock time. Used by the running service."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """A deterministic clock that advances a fixed tick on every reading.

    Used by the suite. The advancing tick is the part worth arguing about: a frozen
    *constant* clock would also be deterministic and is simpler, but it makes every
    duration zero -- so `pipeline_duration_seconds` ships untested -- and, more subtly,
    it makes two steps recorded out of order invisible. Identical timestamps hide the
    bug that timestamps exist to reveal.

    Advancing keeps full determinism, since the same run produces the same timestamps
    every time, while making durations exact arithmetic: a known tick times a known
    number of steps.

    The first reading is `start`; each subsequent one is a tick later.
    """

    def __init__(
        self, start: datetime = DEFAULT_START, tick: timedelta = DEFAULT_TICK
    ) -> None:
        self._next = start
        self._tick = tick

    def now(self) -> datetime:
        current = self._next
        self._next = current + self._tick
        return current
