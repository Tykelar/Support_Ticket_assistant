"""Counters and histograms, held in an in-process registry, rendered as Prometheus text.

**Every metric here is derived from a finished trace, not incremented inline.**
`record_run` walks the same typed steps `GET /tickets/{id}` serves and updates the
registry once, at the orchestrator's single write site
([ADR 0014](../../../docs/adr/0014-metrics-derived-from-the-trace.md)). There is no
instrumentation scattered through `_decide`, so a run and the numbers it produces cannot
drift apart, and the arithmetic is testable: a known `FrozenClock` tick times a known
number of steps is `pipeline_duration_seconds`.

No `prometheus_client` dependency. `Counter` and `Histogram` are a few lines each and
`render()` emits the text exposition format directly. The registry is the seam a real
client would attach at; swapping it is contained (OBSERVABILITY.md).

The families and what moves them are OBSERVABILITY.md's table. `handoffs_total{reason}` is
the one that matters most -- each `HandoffReason` is produced at exactly one place in the
orchestrator, so the breakdown is a real signal rather than a rough grouping.
"""

from collections.abc import Sequence

from support_assistant.enums import HandoffReason, TicketStatus
from support_assistant.tracing.models import (
    GroundingCheck,
    LLMDecision,
    ToolResultStep,
    TraceStep,
)

_DURATION_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
"""Seconds. Wide enough to span the `FakeLLM` (single-digit milliseconds) and a real
model (a second or more) without re-tuning."""

_ITERATION_BUCKETS = (1, 2, 3, 4, 5)
"""`FakeLLM` sits at 3; `MAX_ITERATIONS` is 5. The mass approaching the ceiling predicts
cap handoffs before they happen (OBSERVABILITY.md)."""


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(names: tuple[str, ...], values: tuple[str, ...]) -> str:
    if not names:
        return ""
    pairs = ",".join(
        f'{name}="{_escape(value)}"' for name, value in zip(names, values, strict=True)
    )
    return "{" + pairs + "}"


def _fmt(value: float) -> str:
    """`1` not `1.0`, `0.06` not `0.059999`. Bucket bounds and sums both go through here so
    the rendered text is stable to assert on."""
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


class Counter:
    """A monotonically increasing count, optionally sliced by a fixed set of labels.

    A series is emitted only for a label combination that has actually been seen -- a
    labelled counter with no data is a `# TYPE` line and nothing else, which is the honest
    state before the first run.
    """

    def __init__(self, name: str, documentation: str, labelnames: Sequence[str] = ()) -> None:
        self.name = name
        self.documentation = documentation
        self._labelnames = tuple(labelnames)
        self._values: dict[tuple[str, ...], int] = {}

    def inc(self, amount: int = 1, **labels: str) -> None:
        key = self._key(labels)
        self._values[key] = self._values.get(key, 0) + amount

    def _key(self, labels: dict[str, str]) -> tuple[str, ...]:
        if set(labels) != set(self._labelnames):
            raise ValueError(
                f"{self.name} is labelled {self._labelnames}, got {tuple(labels)}"
            )
        return tuple(str(labels[name]) for name in self._labelnames)

    def render(self) -> str:
        lines = [
            f"# HELP {self.name} {self.documentation}",
            f"# TYPE {self.name} counter",
        ]
        for key, value in self._values.items():
            lines.append(f"{self.name}{_labels(self._labelnames, key)} {value}")
        return "\n".join(lines)


class _Series:
    """One label combination's histogram state: per-bucket counts, an overflow count, the
    running sum, and the total number of observations."""

    def __init__(self, buckets: tuple[float, ...]) -> None:
        self._buckets = buckets
        self.counts = [0] * len(buckets)
        self.overflow = 0
        self.total = 0.0
        self.count = 0

    def observe(self, value: float) -> None:
        for index, bound in enumerate(self._buckets):
            if value <= bound:
                self.counts[index] += 1
                break
        else:
            self.overflow += 1
        self.total += value
        self.count += 1


class Histogram:
    """Observations bucketed by upper bound, plus a sum and a count.

    Buckets render cumulatively with an `le` label, ending in `+Inf`, as the exposition
    format requires. An unlabelled histogram always renders (zeros before any
    observation), so the endpoint is self-describing; a labelled one renders a series per
    combination seen.
    """

    def __init__(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str] = (),
        buckets: Sequence[float] = _DURATION_BUCKETS,
    ) -> None:
        self.name = name
        self.documentation = documentation
        self._labelnames = tuple(labelnames)
        self._buckets = tuple(sorted(buckets))
        self._series: dict[tuple[str, ...], _Series] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        self._series.setdefault(key, _Series(self._buckets)).observe(value)

    def _key(self, labels: dict[str, str]) -> tuple[str, ...]:
        if set(labels) != set(self._labelnames):
            raise ValueError(
                f"{self.name} is labelled {self._labelnames}, got {tuple(labels)}"
            )
        return tuple(str(labels[name]) for name in self._labelnames)

    def render(self) -> str:
        lines = [
            f"# HELP {self.name} {self.documentation}",
            f"# TYPE {self.name} histogram",
        ]
        series = dict(self._series)
        if not series and not self._labelnames:
            series[()] = _Series(self._buckets)

        bucket_labels = self._labelnames + ("le",)
        for key, state in series.items():
            cumulative = 0
            for bound, count in zip(self._buckets, state.counts, strict=True):
                cumulative += count
                lines.append(
                    f"{self.name}_bucket{_labels(bucket_labels, key + (_fmt(bound),))} {cumulative}"
                )
            cumulative += state.overflow
            lines.append(
                f"{self.name}_bucket{_labels(bucket_labels, key + ('+Inf',))} {cumulative}"
            )
            lines.append(f"{self.name}_sum{_labels(self._labelnames, key)} {_fmt(state.total)}")
            lines.append(f"{self.name}_count{_labels(self._labelnames, key)} {state.count}")
        return "\n".join(lines)


class MetricRegistry:
    """The six families of OBSERVABILITY.md, and nothing else.

    Injected, not reached for -- `run_pipeline` takes one and `GET /metrics` reads the one
    `create_app` was handed, the same way `api/` already handles `TicketRepository`. The
    module-level `REGISTRY` is the production default, exactly as `registry.run` and
    `MAX_ITERATIONS` are defaults one layer down.
    """

    def __init__(self) -> None:
        self.tickets_total = Counter(
            "tickets_total", "Tickets that reached a terminal state, by status.", ("status",)
        )
        self.handoffs_total = Counter(
            "handoffs_total", "Handoffs to a human, by reason.", ("reason",)
        )
        self.grounding_violations_total = Counter(
            "grounding_violations_total",
            "Ungrounded literals caught by grounding layer 2, by literal class. Expected: zero.",
            ("literal_class",),
        )
        self.tool_calls_total = Counter(
            "tool_calls_total", "Tool invocations, by tool and outcome.", ("tool", "outcome")
        )
        self.iterations_per_ticket = Histogram(
            "iterations_per_ticket",
            "LLM decisions taken before a ticket terminated.",
            buckets=_ITERATION_BUCKETS,
        )
        self.pipeline_duration_seconds = Histogram(
            "pipeline_duration_seconds",
            "Seconds from the first trace step to the last, by outcome.",
            ("outcome",),
        )
        self._families = (
            self.tickets_total,
            self.handoffs_total,
            self.grounding_violations_total,
            self.tool_calls_total,
            self.iterations_per_ticket,
            self.pipeline_duration_seconds,
        )

    def render(self) -> str:
        """The whole registry as Prometheus text, one family after another, trailing
        newline included."""
        return "\n".join(family.render() for family in self._families) + "\n"


REGISTRY = MetricRegistry()
"""The process-wide registry the running service increments and serves. Tests inject their
own so one test's runs never colour another's `/metrics`."""


def record_run(
    registry: MetricRegistry,
    *,
    status: TicketStatus,
    reason: HandoffReason | None,
    steps: Sequence[TraceStep],
) -> None:
    """Fold one finished run into the registry.

    Called once by `run_pipeline`, **after** `repository.finalise` -- so a run whose
    persist failed is not counted, which is the gap the stranded-ticket gauge
    (deferred, OBSERVABILITY.md) exists to close. Total by construction: it only reads
    closed enums and a closed set of trace step types, so it has nothing to raise on and
    needs no guard around it at the call site.
    """
    registry.tickets_total.inc(status=status.value)
    if reason is not None:
        registry.handoffs_total.inc(reason=reason.value)

    registry.iterations_per_ticket.observe(
        sum(1 for step in steps if isinstance(step, LLMDecision))
    )

    for step in steps:
        if isinstance(step, ToolResultStep):
            registry.tool_calls_total.inc(
                tool=step.tool, outcome="ok" if step.ok else "error"
            )
        elif isinstance(step, GroundingCheck):
            for violation in step.violations:
                registry.grounding_violations_total.inc(
                    literal_class=violation.literal_class.value
                )

    if steps:
        span_seconds = (steps[-1].ts - steps[0].ts).total_seconds()
        registry.pipeline_duration_seconds.observe(span_seconds, outcome=status.value)
