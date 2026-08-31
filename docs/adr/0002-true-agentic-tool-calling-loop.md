# ADR 0002 — A true agentic tool-calling loop

**Status:** Accepted · 2026-08-31

## Context

The brief requires a tool-calling loop with a hard iteration cap, and states plainly that
it is evaluating "the system around the model — the loop, the guardrails, the failure
handling — not prompt engineering".

There are two ways to build this, and they differ in whether the iteration cap guards
anything real.

In a **plan-then-execute** design the model classifies the intent and returns the full
list of tools up front; the orchestrator runs them once. The number of iterations is
`len(plan.tools)` — known before the loop starts, and bounded by construction. An
iteration cap over that is decorative: it can never fire, and no test can make it fire
without fabricating an absurd plan.

In an **agentic loop** the model sees the results of previous tool calls before choosing
the next action. The loop can genuinely fail to terminate — a model that keeps asking for
the same tool, or oscillates between two, runs forever. The cap is the thing standing
between that and an unbounded loop.

## Decision

The LLM client exposes a single decision method:

```python
def decide_next_step(ticket: Ticket, history: list[Observation]) -> ToolCall | Reply | Handoff
```

The orchestrator loops: ask for a step, execute it, append the observation to the
history, ask again. `Reply` and `Handoff` are terminal. The loop is bounded by
`MAX_ITERATIONS` (default 5); exhausting it is itself a handoff reason
(`ITERATION_CAP_EXCEEDED`).

## Consequences

- The iteration cap guards a loop that can actually run away, and is provable by a test:
  a stub client that returns `ToolCall` forever must produce a `handed_off` ticket after
  exactly `MAX_ITERATIONS` iterations.
- The interface matches how real tool-calling APIs work, so `OllamaLLM` (ADR 0006) is a
  drop-in behind the same signature rather than a redesign.
- The model can react to what it found — for example, reading invoices, seeing a failed
  payment, and then fetching the related charging session.
- More moving parts than plan-then-execute: history accumulation, per-iteration tracing,
  and a termination argument that rests on the cap rather than on construction.
- The `FakeLLM` becomes a small state machine over the observation history rather than a
  single classification function. Still keyword-driven and still deterministic
  (ADR 0006), but it has to be written and tested as a stateful decision function.

## Alternatives considered

**Plan-then-execute.** Simpler, trivially terminating, easier to test. Rejected because
it turns an explicitly required guardrail into a no-op and gives a weak answer to the
question the brief says it is actually asking.
