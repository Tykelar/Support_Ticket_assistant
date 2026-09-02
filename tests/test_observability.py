"""The observability package: an in-process metric registry, JSON step logs, and the
recorder hook that feeds them.

Reserved by TESTS.md for `observability/`. Two properties matter most and both are pinned
here:

- **Every metric is derived from a finished trace** (ADR 0014). `record_run` walks the
  same typed steps the API serves, so a run and its numbers cannot disagree, and there is
  no second instrumentation path through `_decide` to keep in sync.
- **`pipeline_duration_seconds` is exact arithmetic on `FrozenClock` ticks**, not a
  wall-clock race -- a known tick times a known number of steps
  ([ADR 0008](../docs/adr/0008-injected-clock-with-advancing-test-double.md)).

The logging assertions attach their own handler to the `support_assistant` logger rather
than using `caplog`, because `observability.logging.configure_logging` sets
`propagate = False` (so the running service does not double-log under uvicorn) and a
`caplog` that ran after any HTTP test would then capture nothing.
"""

import io
import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from support_assistant.clock import DEFAULT_TICK, FrozenClock
from support_assistant.enums import HandoffReason, Intent, LiteralClass, TicketStatus
from support_assistant.observability.logging import (
    JsonLogFormatter,
    log_step,
    ticket_scope,
)
from support_assistant.observability.metrics import (
    Counter,
    Histogram,
    MetricRegistry,
    record_run,
)
from support_assistant.tracing.models import Violation
from support_assistant.tracing.recorder import TraceRecorder

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _samples(text: str, name: str) -> list[str]:
    """The rendered sample lines for one metric -- the `# HELP` / `# TYPE` header lines
    dropped, so a metric with a header but no data reads as an empty list."""
    return [
        line
        for line in text.splitlines()
        if line.startswith(name) and not line.startswith("#")
    ]


@contextmanager
def _captured_logs() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("support_assistant")
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def _replied_trace(clock: FrozenClock) -> list:
    """A minimal happy-path trace: classify, one tool round trip, reply, ground, decide."""
    rec = TraceRecorder(clock)
    rec.intent_classified(Intent.BILLING_QUESTION, ["invoice"])
    rec.llm_decision(1, "tool_call", tool="get_user")
    rec.tool_call("get_user", {"user_id": "u_002"})
    rec.tool_result("get_user", summary={"found": True, "plan": "basic"})
    rec.llm_decision(2, "reply")
    rec.grounding_check(3, [])
    rec.final_decision(TicketStatus.REPLIED)
    return rec.steps


# --------------------------------------------------------------------------------------
# The primitives
# --------------------------------------------------------------------------------------


def test_a_counter_emits_a_series_only_for_label_sets_it_has_seen() -> None:
    counter = Counter("things_total", "Things that happened.", ("kind",))
    counter.inc(kind="a")
    counter.inc(kind="a")
    counter.inc(kind="b")

    text = counter.render()
    assert "# TYPE things_total counter" in text
    assert 'things_total{kind="a"} 2' in text
    assert 'things_total{kind="b"} 1' in text


def test_a_counter_still_describes_itself_with_no_data() -> None:
    # An endpoint that serves nothing reads as "nothing is wrong" (API.md). The header is
    # always emitted; only the sample lines wait for data.
    text = Counter("things_total", "Things.", ("kind",)).render()
    assert "# TYPE things_total counter" in text
    assert _samples(text, "things_total") == []


def test_a_counter_rejects_a_label_it_was_not_declared_with() -> None:
    counter = Counter("things_total", "Things.", ("kind",))
    with pytest.raises(ValueError):
        counter.inc(colour="red")


def test_a_histogram_reports_cumulative_buckets_a_sum_and_a_count() -> None:
    hist = Histogram("sizes", "Sizes.", buckets=(1, 2, 3))
    for value in (0.5, 1.5, 2.5, 9):
        hist.observe(value)

    text = hist.render()
    assert "# TYPE sizes histogram" in text
    assert 'sizes_bucket{le="1"} 1' in text
    assert 'sizes_bucket{le="2"} 2' in text
    assert 'sizes_bucket{le="3"} 3' in text
    assert 'sizes_bucket{le="+Inf"} 4' in text
    assert "sizes_count 4" in text


# --------------------------------------------------------------------------------------
# record_run -- metrics derived from a finished trace
# --------------------------------------------------------------------------------------


def test_a_replied_run_counts_the_ticket_its_tools_and_its_iterations() -> None:
    registry = MetricRegistry()
    steps = _replied_trace(FrozenClock())

    record_run(registry, status=TicketStatus.REPLIED, reason=None, steps=steps)

    text = registry.render()
    assert 'tickets_total{status="replied"} 1' in text
    assert _samples(text, "handoffs_total") == []  # a reply is not a handoff
    assert 'tool_calls_total{tool="get_user",outcome="ok"} 1' in text
    assert "iterations_per_ticket_count 1" in text
    assert 'iterations_per_ticket_bucket{le="2"} 1' in text  # two llm_decision steps


def test_a_handoff_run_is_counted_by_status_and_by_reason() -> None:
    registry = MetricRegistry()
    rec = TraceRecorder(FrozenClock())
    rec.intent_classified(Intent.BILLING_QUESTION, ["invoice"])
    rec.llm_decision(1, "tool_call", tool="get_user")
    rec.tool_call("get_user", {"user_id": "u_005"})
    rec.tool_result("get_user", error=_tool_error("UserNotFound", "no user u_005"))
    rec.final_decision(
        TicketStatus.HANDED_OFF,
        reason=HandoffReason.USER_NOT_FOUND,
        detail="get_user: no user u_005",
    )

    record_run(
        registry,
        status=TicketStatus.HANDED_OFF,
        reason=HandoffReason.USER_NOT_FOUND,
        steps=rec.steps,
    )

    text = registry.render()
    assert 'tickets_total{status="handed_off"} 1' in text
    assert 'handoffs_total{reason="USER_NOT_FOUND"} 1' in text
    assert 'tool_calls_total{tool="get_user",outcome="error"} 1' in text


def test_grounding_violations_are_counted_by_literal_class() -> None:
    registry = MetricRegistry()
    rec = TraceRecorder(FrozenClock())
    rec.grounding_check(
        4,
        [Violation(literal="99.00", literal_class=LiteralClass.NUMBER, reason="unsourced")],
    )
    rec.final_decision(
        TicketStatus.HANDED_OFF,
        reason=HandoffReason.UNGROUNDED_REPLY,
        detail="reply withheld -- unsourced literals: 99.00 (number)",
    )

    record_run(
        registry,
        status=TicketStatus.HANDED_OFF,
        reason=HandoffReason.UNGROUNDED_REPLY,
        steps=rec.steps,
    )

    assert 'grounding_violations_total{literal_class="number"} 1' in registry.render()


def test_pipeline_duration_is_the_span_of_the_trace_in_seconds() -> None:
    registry = MetricRegistry()
    steps = _replied_trace(FrozenClock())

    record_run(registry, status=TicketStatus.REPLIED, reason=None, steps=steps)

    # Seven steps, so six ticks between the first `ts` and the last -- arithmetic, not a
    # tolerance window (ADR 0008).
    assert steps[-1].ts - steps[0].ts == 6 * DEFAULT_TICK
    text = registry.render()
    assert 'pipeline_duration_seconds_count{outcome="replied"} 1' in text
    (sum_line,) = _samples(text, "pipeline_duration_seconds_sum")
    assert sum_line == 'pipeline_duration_seconds_sum{outcome="replied"} 0.06'


def test_the_registry_is_self_describing_before_any_run() -> None:
    text = MetricRegistry().render()
    for name in (
        "tickets_total",
        "handoffs_total",
        "grounding_violations_total",
        "tool_calls_total",
        "iterations_per_ticket",
        "pipeline_duration_seconds",
    ):
        assert f"# TYPE {name} " in text


# --------------------------------------------------------------------------------------
# Structured logs
# --------------------------------------------------------------------------------------


def test_a_step_log_carries_the_bound_ticket_id_and_the_step_ts() -> None:
    rec = TraceRecorder(FrozenClock())
    rec.tool_call("get_user", {"user_id": "u_002"})
    (step,) = rec.steps

    with _captured_logs() as stream, ticket_scope("t_abc"):
        log_step(step)

    (line,) = _lines(stream)
    assert line["event"] == "tool_call"
    assert line["ticket_id"] == "t_abc"
    assert line["level"] == "info"
    assert line["ts"] == step.ts.isoformat().replace("+00:00", "Z")
    assert line["tool"] == "get_user"


def test_a_tool_call_log_carries_exactly_the_keys_that_step_has() -> None:
    """The field set, not just a field.

    `_describe`'s default branch derives a line's fields from the step model itself, so
    nothing declares this shape anywhere -- which is how OBSERVABILITY.md came to document
    an `iteration` key on a `tool_call` line. Only `llm_decision` carries `iteration`; a
    tool call carries `args`. Asserting the whole key set is what makes the documented
    line and the emitted one the same claim.
    """
    rec = TraceRecorder(FrozenClock())
    rec.tool_call("get_invoices", {"user_id": "u_004"})
    (step,) = rec.steps

    with _captured_logs() as stream, ticket_scope("t_abc"):
        log_step(step)

    (line,) = _lines(stream)
    assert set(line) == {"ts", "level", "event", "ticket_id", "tool", "args"}
    assert line["args"] == {"user_id": "u_004"}


def test_no_step_log_carries_the_customers_words() -> None:
    """`logging.py` promises `subject` and `body` are never logged. Because the default
    branch dumps whatever fields a step model declares, that promise is today a property
    of the *trace models* rather than of this module: a field added to any step would
    reach the log aggregator on the next deploy with nothing objecting.

    This is the assertion that makes the promise the module's own -- the same shape of
    guard as `test_clock.py`'s grep for `datetime.now(`.
    """
    clock = FrozenClock()
    steps = _replied_trace(clock)
    rec = TraceRecorder(clock)
    rec.tool_result("get_invoices", error=_tool_error("NoDataAvailable", "no invoices"))
    rec.final_decision(
        TicketStatus.HANDED_OFF,
        reason=HandoffReason.DATA_NOT_FOUND,
        detail="get_invoices: user u_004 has no invoices",
    )
    steps += rec.steps

    with _captured_logs() as stream, ticket_scope("t_abc"):
        for step in steps:
            log_step(step)

    logged_keys = {key for line in _lines(stream) for key in line}
    assert not logged_keys & {"subject", "body"}


def test_a_handoff_step_logs_at_warning_with_the_reason() -> None:
    rec = TraceRecorder(FrozenClock())
    rec.final_decision(
        TicketStatus.HANDED_OFF,
        reason=HandoffReason.USER_NOT_FOUND,
        detail="get_user: no user u_005",
    )
    (step,) = rec.steps

    with _captured_logs() as stream, ticket_scope("t_abc"):
        log_step(step)

    (line,) = _lines(stream)
    assert line["event"] == "handoff"
    assert line["level"] == "warning"
    assert line["reason"] == "USER_NOT_FOUND"


def test_the_ticket_id_scope_does_not_leak_past_the_run() -> None:
    rec = TraceRecorder(FrozenClock())
    rec.llm_decision(1, "reply")
    (step,) = rec.steps

    with _captured_logs() as stream:
        with ticket_scope("t_abc"):
            pass
        log_step(step)  # emitted outside any scope

    (line,) = _lines(stream)
    assert "ticket_id" not in line or line["ticket_id"] is None


# --------------------------------------------------------------------------------------
# The recorder hook that feeds the logs
# --------------------------------------------------------------------------------------


def test_the_recorder_calls_the_hook_once_per_step_with_the_step() -> None:
    seen: list = []
    rec = TraceRecorder(FrozenClock(), on_step=seen.append)

    rec.intent_classified(Intent.UNKNOWN, [])
    rec.final_decision(
        TicketStatus.HANDED_OFF,
        reason=HandoffReason.UNSUPPORTED_INTENT,
        detail="nothing matched",
    )

    assert [step.type for step in seen] == ["intent_classified", "final_decision"]
    assert seen == rec.steps


def test_the_hook_is_optional_and_off_by_default() -> None:
    rec = TraceRecorder(FrozenClock())  # no on_step
    rec.llm_decision(1, "reply")
    assert [step.type for step in rec.steps] == ["llm_decision"]


# --------------------------------------------------------------------------------------
# Local helpers
# --------------------------------------------------------------------------------------


def _tool_error(type_: str, message: str):
    from support_assistant.tracing.models import ToolError

    return ToolError(type=type_, message=message)


def test_a_run_that_took_no_decisions_is_not_counted_as_one_iteration() -> None:
    """An `unknown` intent hands off before the loop, so no `llm_decision` step exists.
    Without a zero bucket that run lands in `le="1"` and reads as a one-iteration reply --
    the histogram would say the loop ran when it never started.
    """
    registry = MetricRegistry()
    rec = TraceRecorder(FrozenClock())
    rec.intent_classified(Intent.UNKNOWN, [])
    rec.final_decision(
        TicketStatus.HANDED_OFF,
        reason=HandoffReason.UNSUPPORTED_INTENT,
        detail="intent classified unknown",
    )

    record_run(
        registry,
        status=TicketStatus.HANDED_OFF,
        reason=HandoffReason.UNSUPPORTED_INTENT,
        steps=rec.steps,
    )

    text = registry.render()
    assert 'iterations_per_ticket_bucket{le="0"} 1' in text
    assert "iterations_per_ticket_sum 0" in text


def test_a_counter_keeps_every_increment_under_concurrent_writers() -> None:
    """`record_run` runs in the background threadpool, so two runs can finish at once and
    `inc` is a read-modify-write. `SqliteTicketRepository` takes a lock for the same
    reason.

    Under the GIL a lost update is rare rather than certain, so this test is a regression
    guard on the locked implementation, not a demonstration of the unlocked bug.
    """
    counter = Counter("things_total", "Things that happened.", ("kind",))

    def _bump() -> None:
        for _ in range(2_000):
            counter.inc(kind="a")

    threads = [threading.Thread(target=_bump) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert 'things_total{kind="a"} 16000' in counter.render()
