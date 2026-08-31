# ADR 0009 — Absent data is a handoff, not a reply

**Status:** Accepted · 2026-08-31

## Context

A user exists. They ask "do I owe anything?". `get_invoices` finds no rows.

There is a truthful, fully-grounded reply available here: *you have no invoices*. That is
not invented data — the absence is itself a tool result, and it satisfies the letter of
the grounding rule in [ADR 0004](0004-two-layer-grounding-enforcement.md). Handing this
off means a human is paged to write a sentence the system could have written.

So the obvious implementation returns an empty list and lets a template say so. Choosing
not to needs recording, because a future reader will look at the handoff and reasonably
think it is a bug.

The argument against the obvious path is that **zero rows is ambiguous**, and the tool
cannot tell which case it is in:

- the user genuinely has no invoices;
- the invoices exist but have not synced yet;
- a filter, join, or identity mapping is broken upstream.

The first deserves the reply. The second and third produce a confident *"you have no
invoices"* sent to a customer whose invoices are sitting right there — a wrong statement
about someone's billing, which is the failure mode this system is built to avoid. Nothing
in the tool result distinguishes them.

The brief also names "the requested data doesn't exist" as an explicit handoff trigger, so
the cautious reading is the one it endorses.

## Decision

An empty result is a failure, not a success. `get_charging_sessions` and `get_invoices`
raise `NoDataAvailable` rather than returning `[]`, and the orchestrator converts that to
`DATA_NOT_FOUND`.

**Narrowed to where it makes sense.** This applies only to the two collection-returning
tools. `get_user` returns a single record and cannot be "empty" — a user either exists
(success) or does not (`UserNotFound` → `USER_NOT_FOUND`). Conflating the two would lose
the distinction the brief draws between a missing user and missing data.

## Consequences

- No reply is ever built on an absence, so the sync-lag and broken-join cases cannot
  produce a wrong statement about a customer's billing.
- `u_004` in the fixtures — a real user with zero sessions and zero invoices — is the
  case that exercises this, and it is the brief's required handoff-on-missing-data test.
- A human is occasionally paged for a ticket they could have answered in one line. That is
  the cost, and it is deliberate. It shows up as `DATA_NOT_FOUND` in the handoff-rate
  breakdown ([OBSERVABILITY.md](../../src/support_assistant/observability/OBSERVABILITY.md)),
  so the cost is visible rather than hidden.
- Tools that legitimately return nothing have no way to say so. Should a fourth tool be
  added where emptiness is unambiguous, it should not inherit this rule blindly.

**The condition under which this should flip:** a data source that can distinguish "no
rows because none exist" from "no rows because something is wrong" — a synced-at
timestamp, a completeness flag, a source that fails loudly rather than returning empty.
With that signal, replying truthfully about an absence is strictly better than handing
off, and this ADR should be superseded rather than worked around.

## Alternatives considered

**Return `[]` and template "you have no invoices".** Answers a question the system can
genuinely answer, and reduces needless handoffs. Rejected because the fixtures — and a
real data source of this shape — cannot distinguish absence from breakage, and the
downside case is a confidently wrong statement about money.

**Let the model decide.** Pass the empty list into the history and let
`decide_next_step` choose between replying and handing off. Rejected: it moves a safety
decision from deterministic code into the component whose judgement the guardrails exist
to bound, and it would behave differently under `FakeLLM` and `OllamaLLM`.
