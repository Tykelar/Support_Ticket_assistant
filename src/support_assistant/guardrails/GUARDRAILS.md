# Guardrails

Three mechanisms that keep the pipeline from doing the wrong thing: a bounded loop, a
closed set of handoff rules, and two-layer grounding.

Guardrails **report**; the orchestrator **decides**. Nothing in this package writes a
terminal state ([ADR 0005](../../../docs/adr/0005-fail-closed-to-human-handoff.md)).

---

## 1. Iteration cap

`MAX_ITERATIONS`, default **5**, from the environment. Enforced by the orchestrator's
`for ... else`, so there is no path where exhausting the loop yields a reply.

**Why 5.** `FakeLLM` terminates in at most three iterations. Five leaves headroom for a
fourth tool and is low enough that a runaway loop is cheap.

**Why it is a real guardrail.** Under a plan-then-execute design the iteration count is
known before the loop starts and a cap can never fire. Because the model sees prior results
before choosing its next action
([ADR 0002](../../../docs/adr/0002-true-agentic-tool-calling-loop.md)), it can genuinely
loop forever — asking for the same tool repeatedly, or oscillating between two.

`tests/test_iteration_cap.py` drives a stub that returns `ToolCall` forever and asserts the
ticket hands off after exactly `MAX_ITERATIONS` iterations — on the count, so an off-by-one
that ran six times is caught. No timeout needed: the bound is structural, so a hang would
itself be the failure.

---

## 2. Handoff rules

The brief requires a handoff when the user or requested data doesn't exist, the intent is
outside the supported categories, or any step fails. That maps to six reasons.

| Reason | Trigger | Detected by |
|---|---|---|
| `USER_NOT_FOUND` | no fixture record for the `user_id` | `UserNotFound` from any tool |
| `DATA_NOT_FOUND` | user exists, no rows of the needed kind | `NoDataAvailable` from a collection tool ([ADR 0009](../../../docs/adr/0009-absent-data-is-a-handoff.md)) |
| `UNSUPPORTED_INTENT` | intent is `unknown`, including a keyword tie | classification, pre-loop |
| `TOOL_ERROR` | malformed fixture, unregistered tool, or **any** unhandled exception | typed error or the catch-all |
| `ITERATION_CAP_EXCEEDED` | loop hit the cap | `for ... else` |
| `UNGROUNDED_REPLY` | an unsourced literal in the finished reply | grounding layer 2 |

The enum is closed, so every handoff is machine-readable and handoff-rate **by reason** is
countable — the single most informative signal this system emits
([OBSERVABILITY.md](../observability/OBSERVABILITY.md)). Adding a rule is a bounded change:
a member, a typed failure, a mapping in the orchestrator, a test.

Every handoff writes a `final_decision` step carrying the reason **and its detail** — which
user id was missing, which tool raised, which literal was ungrounded.

### The bias this creates

A system tuned this way hands off tickets a human would have found answerable. That is the
intended trade in a domain where a wrong reply about someone's money costs more than a slow
one. Handoff-rate-by-reason is how you would find over-eager rules and tune them
deliberately rather than discovering the bias by accident.

---

## 3. Grounding — two layers

> Inventing data the tools didn't return is the one unforgivable bug in this domain.

Full reasoning in [ADR 0004](../../../docs/adr/0004-two-layer-grounding-enforcement.md).
The short version: a structural guarantee is airtight but is a property of the *fake*, and
it disappears silently the moment a real model writes prose.

### Layer 1 — structural (`FactSet`)

Tool results are projected into a typed `FactSet`, and templates interpolate only from it,
so there is no code path from a template to a value no tool returned. Under `FakeLLM` this
alone makes invented data impossible.

The projection is lossy on purpose: it drops dates, plan tier and language, so a reply
cannot state them even by accident.

Four helpers expose the facts, one per literal class, and they are exactly what layer 2
compares against — `allowed_numbers()` (as `Decimal`, so `42.10` and `42.1` are one fact),
`allowed_identifiers()`, `allowed_statuses()`, and `allowed_entities()`, the odd one out
described in layer 2's table below.

### Layer 2 — post-hoc verification (`GroundingChecker`)

Runs on the **rendered reply**, unconditionally, regardless of which client produced it.

```python
GroundingChecker.extract(reply: str, facts: FactSet) -> list[Literal]
GroundingChecker.verify(reply: str, facts: FactSet, template: Template) -> GroundingResult
```

`verify` checks each extracted literal against the `FactSet` and
`template.TEMPLATE_SAFE_LITERALS`, returning both the `literals` it checked and one
`Violation` per unsourced one. Returning both from one call is what keeps the trace's
`literals_checked` from being a second, separate reading of the reply.

`extract` takes the facts because two kinds of span are masked before scanning.

| Class | Extraction | Normalisation |
|---|---|---|
| Numbers | `\d+(?:[.,]\d+)?` | parsed to `Decimal`, so `42.10`, `42,10` and `42.1` compare equal |
| Identifiers | fixture id patterns (`inv_`, `sess_`, `u_`) — **masked out first**, so the digits inside `inv_204` are not re-read as an amount | exact match |
| Status words | the closed `InvoiceStatus` / `SessionStatus` vocabularies, with the pattern **built from the enum members** rather than retyped | case-folded; the `Violation` still records the spelling the reply used |
| Sourced entities | not extracted — **masked out first**, like identifiers. Station names and the user's name, from `allowed_entities()` | n/a. A station the tools returned is sourced text: `A1 Norte` is not an ungrounded `1`. Only strings the `FactSet` holds are masked, so an invented station's digits are still scanned |

A literal of a class `verify` has no rule for is treated as ungrounded, so adding a
`LiteralClass` member fails closed rather than crashing the guardrail.

Any literal not accounted for is a `Violation`: `handed_off` / `UNGROUNDED_REPLY`, the
reply discarded, the offending literals recorded in the trace as evidence.

### `TEMPLATE_SAFE_LITERALS`

A template's static prose may contain numbers — "within 3 business days". Those are not
facts and will never be in a `FactSet`, so each template declares them.

It is a **numbers** allowlist and nothing else: unioned into the allowed numbers only,
never the identifiers or the status words. A template that could whitelist `failed` would
be switching off half the check on its own authority.

Per-template and small by design, so adding a number to prose is a visible, reviewable act.

### What layer 2 does not catch

Stated plainly, because a guardrail whose limits are undocumented is one people over-trust.

- **Sourcing, not truth.** It would not catch *"your invoice of EUR 42.10 was paid"* when
  42.10 is real but the status is `failed`. Layer 1 covers this today because status words
  are enumerated `FactSet` values.
  [Roadmap](../../../docs/ROADMAP.md#semantic-grounding).
- **Open-vocabulary entities.** An invented station name or currency is none of the three
  extracted shapes. Under `FakeLLM` neither can occur, because layer 1 makes them
  unreachable; this is the main gap a real provider would widen. Masking narrows the *false
  positive* side of this, not the false negative.
  [Roadmap](../../../docs/ROADMAP.md#entity-coverage-in-grounding-layer-2).
- **Row counts are facts, so small integers are cheap.** `allowed_numbers()` includes the
  number of invoices and sessions, because a reply may say "all 3 of your invoices". For a
  user with three invoices, a bare `3` anywhere in the prose passes.
- **Multi-part numerals split.** `1,234.56` extracts as `1,234` and `56`, because the comma
  is read as a decimal separator (which is what makes `42,10` work). Both halves fail
  closed, so nothing unsourced gets through, but the violation reads oddly. No fixture
  amount reaches four figures.

### How it is proved

`test_grounding.py::test_ungrounded_literal_is_caught` builds a real `Template` — the
production dataclass, real field selection, real safe-literal set — with one amount
doctored into its prose, and asserts the `Violation`. A control renders the same template
undoctored, so the failure is the injected amount rather than something incidental.

Its other half,
`test_an_ungrounded_literal_withholds_the_reply_and_hands_the_ticket_off`, runs that
template through the real orchestrator and asserts what a customer would have received:
`reply is None`, `handed_off` / `UNGROUNDED_REPLY`, the literal in the trace. Catching the
literal and sending the reply anyway would pass the first half alone. A second control
replies honestly through the same path, so handing off every ticket could not pass either.

---

## Structure

```
guardrails/
  __init__.py
  factset.py       FactSet + InvoiceFact / SessionFact, projection, the allowed_* helpers
  grounding.py     GroundingChecker, Literal, GroundingResult, the extraction rules
  limits.py        MAX_ITERATIONS + max_iterations()
  GUARDRAILS.md    this file
```

Two types this package produces are defined outside it, so `domain` does not end up
importing a component: `HandoffReason` and `LiteralClass` are in `enums.py`, and
`Violation` is in `tracing/models.py` with the step that carries it
([ADR 0011](../../../docs/adr/0011-shared-vocabulary-below-the-components.md)).
