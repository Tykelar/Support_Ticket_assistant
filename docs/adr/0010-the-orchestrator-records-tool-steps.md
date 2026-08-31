# ADR 0010 — The orchestrator records tool steps; the registry only dispatches

**Status:** Accepted · 2026-08-31

## Context

Two documents disagreed, and neither of them was complete.

[TOOLS.md](../../src/support_assistant/tools/TOOLS.md) justified the registry partly as a
tracing chokepoint: every tool call passes through one function, so `tool_call` and
`tool_result` steps could be recorded in one place "rather than at three call sites".
[PIPELINE.md](../../src/support_assistant/pipeline/PIPELINE.md) built the `TraceRecorder`
in the orchestrator, recorded both steps there, and never passed it to the registry.

The disagreement surfaced a worse gap. In the pipeline's control flow the tool call
raises on failure:

```python
trace.tool_call(step.tool, step.args)
result = registry.run(step.tool, step.args)   # may raise
trace.tool_result(step.tool, summarise(result))
```

A raising tool jumps straight past the line that would record it, so the `ok: false` step
was written by nothing — while
[TRACEABILITY.md](../../src/support_assistant/tracing/TRACEABILITY.md) presents exactly
that step as the first thing a support agent reads when a ticket went wrong, and the
brief requires each tool call to be traced with its result. The most important step in
the trace was the one no component owned.

The "three call sites" the chokepoint argument worried about also do not exist. The loop
dispatches by name through `registry.run`; there is one call site, and adding a fourth
tool does not add another.

## Decision

**The orchestrator records; the registry dispatches and validates.**

The tool-call branch of the loop wraps the dispatch, so both outcomes are recorded at the
single call site:

```python
case ToolCall():
    trace.tool_call(step.tool, step.args)
    try:
        result = registry.run(step.tool, step.args)
    except Exception as exc:
        trace.tool_result(step.tool, error=ToolError(type(exc).__name__, str(exc)))
        raise
    trace.tool_result(step.tool, summarise(result))
    history.append(Observation(step, result))
```

**The catch is `Exception`, deliberately broad.** "Any step fails" is one of the brief's
three handoff triggers, and an unanticipated exception is the case that cannot be
enumerated — it is also the one where a reader most needs to know which tool was in
flight when things broke. Narrowing this to a base class over the three typed tool errors
would leave the most confusing failures as the ones the trace stays silent about. The
`raise` is unchanged, so the typed handlers downstream still map `UserNotFound` →
`USER_NOT_FOUND`, `NoDataAvailable` → `DATA_NOT_FOUND`, and everything else →
`TOOL_ERROR` ([ADR 0005](0005-fail-closed-to-human-handoff.md)).

## Consequences

- **`tools/` imports nothing from `tracing/`.** The registry stays a dispatch table with
  a schema per entry: no recorder, no clock, no summariser. That is what keeps it
  testable without assembling a trace, and it preserves the rule in
  [ARCHITECTURE.md](../../ARCHITECTURE.md) that `llm`, `tools` and `guardrails` know
  nothing about each other and meet only in the orchestrator.
- **A failed tool call now always produces a `tool_result` step**, and a tool name the
  registry rejects produces a `tool_call` step followed by a failed `tool_result` — which
  is precisely the model-chose-versus-system-did divergence the trace exists to show.
- The orchestrator carries a little more mechanism in its loop body. That is the right
  place for it: recording what happened is orchestration, not data access.
- `TOOLS.md`'s second justification for the registry is gone. Two remain — containment
  and argument validation — and they were always the load-bearing ones.

## Alternatives considered

**The registry holds the `TraceRecorder`** and records both steps around its own dispatch.
This is what `TOOLS.md` originally claimed and it is genuinely tempting: one place, no
wrapping in the loop. Rejected because a registry that traces needs a recorder, a clock
and the summariser, at which point `tools/` depends on `tracing/` and stops being a
dispatch table. It also makes every tool test require a trace recorder to exist.

**The registry returns a failure result instead of raising.** The orchestrator then
inspects the result and decides. Rejected as the worst of the three: it converts a raise
into a return value that a caller must remember to check, and a forgotten check produces
a reply built from a failed tool call. That is the fail-open shape
[ADR 0005](0005-fail-closed-to-human-handoff.md) exists to prevent.

**Record only the successful call, and let `final_decision` carry the failure.** Cheapest,
and the reason is not lost. Rejected because the reason says *what kind* of failure, not
which tool or which arguments produced it — and reconstructing that from a
`final_decision` alone is the reconstruction work the trace is supposed to have already
done.
