# ADR 0011 — Shared vocabulary lives below the components, not inside one

**Status:** Accepted · 2026-09-01

## Context

[ARCHITECTURE.md](../../ARCHITECTURE.md) draws the dependency direction as
`everything --> domain / enums / clock`, with `llm`, `tools` and `guardrails` knowing
nothing about each other. The code did not match it. `domain.py` imported two component
packages:

```python
from support_assistant.guardrails.handoff import HandoffReason   # domain -> guardrails
from support_assistant.tracing.models import TraceStep           # domain -> tracing
```

The second is inherent: a `Ticket` carries its trace, so `domain` must name `TraceStep`.
The first was not. `HandoffReason` is an enum, and it is already listed in
ARCHITECTURE.md section 4 among the contracts fixed across the system — beside `Intent`
and `TicketStatus`, which both live in `enums.py`. It was the only one that did not.

The cost was not theoretical. Because `domain -> tracing.models -> guardrails.grounding`,
any guardrails module that imported `FactSet` (which needs `domain`) could not live in the
same file as `Violation`. So `GroundingChecker` was pushed into a separate `checker.py`,
leaving `grounding.py` holding a single 20-line model — and the reason had to be explained
in four places: three module docstrings and GUARDRAILS.md. Four paragraphs restating one
constraint is a codebase arguing with its own structure, and it violates
[ADR 0007](0007-component-packages-with-colocated-docs.md)'s rule that the *why* is
written once, in the ADR.

## Decision

Vocabulary that more than one component names lives at the bottom, in `enums.py`. Types
that are payloads of a trace step live with the trace.

- `HandoffReason` moves from `guardrails/handoff.py` to `enums.py`, and is re-exported
  by `domain` like the other five enums. `guardrails/handoff.py` is deleted; the typed
  failures its docstring pointed at already live in `tools/errors.py`.
- `Violation` moves from `guardrails/grounding.py` to `tracing/models.py`. It is the
  evidence payload of the `grounding_check` step, and
  [TRACEABILITY.md](../../src/support_assistant/tracing/TRACEABILITY.md) already owns its
  JSON shape.
- `LiteralClass` — the `number` / `identifier` / `status` vocabulary that was previously a
  bare `str` with the values written in a docstring — is a `StrEnum` in `enums.py`, named
  by both the checker and `Violation`.
- With `Violation` gone from `guardrails/`, the cycle disappears. `checker.py` moves back
  to `grounding.py` and the package holds one grounding module instead of two.

The resulting order, each layer importing only what is beneath it:

```
enums.py                    imports nothing
tracing/models.py           enums
domain.py                   enums, tracing.models
guardrails/factset.py       domain, enums
guardrails/grounding.py     factset, tracing.models, enums
```

## Consequences

- `domain.py` no longer imports a component package. ARCHITECTURE.md's arrow is true.
- `guardrails/` goes from five modules to four, and the four import-cycle paragraphs are
  deleted rather than reworded.
- One new cross-component edge, `guardrails -> tracing.models`, for `Violation`.
  Acceptable: `domain` already treats `tracing.models` as a low-level shared contract, and
  ARCHITECTURE.md's mutual-ignorance rule names only `llm`, `tools` and `guardrails`. The
  alternative — keeping `Violation` in `guardrails` — is what put an entire component
  underneath `domain`, which is strictly worse.
- Only `tracing/models.py` is constrained, not all of `tracing/`. `summarise.py` reads
  domain records and legitimately sits above them. The invariant is about the one module
  `domain` names.
- `tests/test_layering.py` enforces all of this by parsing the imports, in the style of
  the existing wall-clock guard in `test_clock.py`
  ([ADR 0008](0008-injected-clock-with-advancing-test-double.md)). It excludes
  `if TYPE_CHECKING:` blocks, so `llm/templates.py` naming `FactSet` for type-checking
  stays legal, and it records the one sanctioned `llm -> tools` import by name so a
  *second* one still fails.

## Alternatives considered

**Leave it and deduplicate the prose.** Cheapest, and the docs half-defended the status
quo already ([LLM.md](../../src/support_assistant/llm/LLM.md) noted the transitive pull).
Rejected: it keeps a structure whose only justification is the workaround it forces, and
the split modules would keep inviting the same question.

**Move `Violation` into `enums.py` or `domain.py`.** It is a model, not an enum, so
`enums.py` is wrong. `domain.py` cannot hold it: `tracing.models` needs `Violation`, and
`domain` imports `tracing.models`, so it would be the same cycle in the other direction.

**Give `Ticket` no `trace` field, breaking `domain -> tracing` too.** That would make
`domain` a pure leaf. Rejected: the trace being part of the ticket is the point of
[ADR 0005](0005-fail-closed-to-human-handoff.md) and the storage schema, and a
`TicketWithTrace` assembled at the edges buys layering purity at the cost of the model.
