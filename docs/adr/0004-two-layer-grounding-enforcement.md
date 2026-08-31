# ADR 0004 — Two-layer grounding enforcement

**Status:** Accepted · 2026-08-31

## Context

> The reply may only contain facts that came from tool results. **Inventing data the
> tools didn't return is the one unforgivable bug in this domain.**

The brief names this as the single worst failure mode, so it deserves a mechanism rather
than an intention.

The obvious mechanism is structural: make the reply renderer physically incapable of
emitting a value that no tool returned. That works, and it is airtight — but only as long
as a template is doing the rendering. It is a property of the *fake* LLM, not of the
system. The moment a real model generates free prose (ADR 0006), the guarantee is gone,
and it disappears silently: nothing fails, the reply just starts being trustworthy for a
reason that no longer holds.

A guardrail whose strength depends on which implementation happens to be plugged in is
not really a guardrail.

## Decision

Enforce grounding twice, at two different layers.

**Layer 1 — structural.** Tool results are projected into a typed `FactSet`. Templates
interpolate only from the `FactSet`. There is no code path by which the template renderer
can reach a value that did not come from a recorded tool result.

**Layer 2 — post-hoc verification.** After rendering, and regardless of which LLM
produced the text, `GroundingChecker.verify(reply, factset)` re-reads the finished reply
and checks every factual literal in it against the `FactSet`:

- numeric literals (amounts, kWh, counts, dates) must appear in
  `FactSet.allowed_literals()`, or in the small explicit `TEMPLATE_SAFE_LITERALS` set a
  template declares for its own static prose (e.g. "3 business days");
- entity strings (station names, invoice ids, the user's name) must likewise be present
  in the `FactSet`.

Any violation fails closed: no reply is sent, the ticket is `handed_off` with
`UNGROUNDED_REPLY`, and the offending literal is recorded in the trace as evidence
(ADR 0005).

## Consequences

- The protection survives swapping `FakeLLM` for `OllamaLLM`. Layer 2 is provider-
  agnostic because it inspects output, not the thing that produced it.
- It is testable in a way a reviewer can see: a deliberately doctored template that
  injects an amount absent from the tool results must produce a `handed_off` ticket.
  That test is the proof the unforgivable bug cannot ship.
- Layer 2's allowlist is explicit and auditable rather than heuristic. The cost is that
  adding static prose containing a number to a template also requires adding it to that
  template's `TEMPLATE_SAFE_LITERALS` — a deliberate speed bump on exactly the change
  that would otherwise erode the guarantee.
- **Known limitation, stated plainly:** the checker verifies that every literal is
  *sourced*, not that the sentence around it is *true*. It would not catch "your invoice
  of €42.10 was paid" when €42.10 is real but the status is `failed`. Layer 1 covers that
  case today, because status words are themselves enumerated `FactSet` values rather than
  free text. Under a real LLM, semantic faithfulness would need a separate check — an
  entailment test against the `FactSet`. Out of scope here and recorded in the README.
- Two mechanisms to maintain, and some duplicated intent between them. That redundancy is
  the point.

## Alternatives considered

**Structural only.** Clean, provable, cheap. Rejected: it is a property of the fake, and
it fails silently and invisibly the moment a real model is wired in.

**Post-hoc only.** One mechanism, provider-agnostic, less duplication. Rejected: it makes
a claim-extraction routine the single point of failure for the worst bug in the domain,
with nothing behind it when the extractor is leaky.
