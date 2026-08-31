# ADR 0005 — Fail closed to human handoff

**Status:** Accepted · 2026-08-31

## Context

> The pipeline must hand off to a human (status `handed_off`, no reply sent) when: the
> user or requested data doesn't exist, the intent is outside the supported categories,
> or any step fails. **Failing closed beats replying wrong.**

The risk in implementing this is scattering the decision. If each component decides for
itself when to give up — a tool returning `None`, the loop swallowing an exception, the
renderer emitting an apology string — then "why was this handed off?" has as many answers
as there are code paths, and some failures produce a reply anyway.

## Decision

Handoff is a single, typed, centralised outcome.

- One enum, `HandoffReason`, with a closed set of members:
  `USER_NOT_FOUND`, `DATA_NOT_FOUND`, `UNSUPPORTED_INTENT`, `TOOL_ERROR`,
  `ITERATION_CAP_EXCEEDED`, `UNGROUNDED_REPLY`.
- Components do not decide to hand off. They raise or return a typed failure; the
  pipeline orchestrator is the only place that converts one into a terminal outcome.
- The orchestrator wraps the whole run in a catch-all. Any unanticipated exception
  becomes `TOOL_ERROR` with the exception recorded — an unhandled crash must never leave
  a ticket in `processing`, and must never produce a reply.
- A handed-off ticket has `reply == None`. Not an empty string, not a polite holding
  message. The API contract makes the absence explicit.
- Every handoff writes a `final_decision` trace step carrying the reason and its
  supporting detail (which user id was missing, which tool raised, which literal was
  ungrounded).

## Consequences

- "Why was this handed off?" always has exactly one answer, and it is machine-readable.
- The reason enum is directly countable, which is what makes the operational metrics in
  `OBSERVABILITY.md` possible — handoff rate broken down by reason is the single most
  informative signal about whether this system is working.
- Adding a new handoff rule is a small, obvious change: add an enum member, raise the
  typed failure, add a test. This is close to what the live review session is likely to
  ask for.
- Deliberate bias toward false handoffs. A system tuned this way will hand off tickets a
  human would have found answerable. That is the intended trade in this domain, and the
  handoff-rate-by-reason metric is how you would find and tune the over-eager rules.
- The catch-all obscures stack traces from the API surface, so they are written to the
  trace and the structured log instead.

## Alternatives considered

**Per-component handoff decisions.** Less indirection, each component handles what it
knows about. Rejected: the reason for a handoff becomes implicit in control flow, which
is exactly what requirement 5 forbids.

**Best-effort replies with a hedge** ("I couldn't find your invoice, but generally…").
Rejected outright — it is the failure mode the brief calls unforgivable, wearing a
politer costume.
