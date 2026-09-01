# Guardrails

Three mechanisms that keep the pipeline from doing the wrong thing: a bounded loop, a
closed set of handoff rules, and two-layer grounding.

Guardrails **report**; the orchestrator **decides**. Nothing in this package writes a
terminal state ([ADR 0005](../../../docs/adr/0005-fail-closed-to-human-handoff.md)).

---

## 1. Iteration cap

`MAX_ITERATIONS`, default **5**, from the environment.

Enforced structurally in the orchestrator's `for ... else`: the `else` branch runs only
when the loop finished without `break`, and it hands off with `ITERATION_CAP_EXCEEDED`.
There is no path where exhausting the loop yields a reply.

**Why 5.** `FakeLLM` terminates in at most three iterations (`get_user`, then the
intent's data tool, then `Reply`). Five leaves headroom for a fourth tool without
immediately touching the ceiling, and is low enough that a runaway loop is cheap. It is
configurable because the right value depends on the model, not on the architecture.

**Why it is a real guardrail here.** Under a plan-then-execute design the iteration count
is known before the loop starts and a cap can never fire. Because the model sees prior
results before choosing its next action ([ADR 0002](../../../docs/adr/0002-true-agentic-tool-calling-loop.md)),
it can genuinely loop forever — asking for the same tool repeatedly, or oscillating
between two. The cap is what stands between that and an unbounded run.

**How it is proved.** `tests/test_iteration_cap.py` drives a stub client that returns
`ToolCall` forever and asserts the ticket reaches `handed_off` with
`ITERATION_CAP_EXCEEDED` after exactly `MAX_ITERATIONS` iterations — on the count, not
just the outcome, so an off-by-one that ran six times is caught. The test needs no
timeout: the bound is structural, so a hang would itself be the failure.

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

Two consequences of the enum being closed:

- Every handoff is machine-readable, so handoff rate **by reason** is countable. That
  breakdown is the single most informative production signal this system emits
  ([OBSERVABILITY.md](../observability/OBSERVABILITY.md)).
- Adding a rule is a bounded change: add a member, raise the typed failure, map it in the
  orchestrator, add a test.

Every handoff writes a `final_decision` trace step carrying the reason **and its
supporting detail** — which user id was missing, which tool raised, which literal was
ungrounded. A reason without detail explains the category but not the incident.

### The bias this creates

A system tuned this way hands off tickets a human would have found answerable. Ambiguous
classification, a user with no invoices, a template with an undeclared literal — all
become handoffs. That is the intended trade in a domain where a wrong reply about
someone's money costs more than a slow one. Handoff-rate-by-reason is how you would find
rules that are over-eager and tune them deliberately, rather than discovering the bias by
accident.

---

## 3. Grounding — two layers

> Inventing data the tools didn't return is the one unforgivable bug in this domain.

Full reasoning in [ADR 0004](../../../docs/adr/0004-two-layer-grounding-enforcement.md).
The short version: a structural guarantee is airtight but is a property of the *fake*, and
it disappears silently the moment a real model writes prose. So there are two layers.

### Layer 1 — structural (`FactSet`)

Tool results are projected into a typed `FactSet`. Templates interpolate only from it.

```python
facts = FactSet.from_observations(history)
facts.allowed_literals()   # {"Ben", "inv_204", "42.10", "EUR", "failed", "3", ...}
```

There is no code path from a template to a value that no tool returned. Under `FakeLLM`
this alone makes invented data impossible.

`allowed_literals()` is the whole-facts view — every fact as text, for a reader, a test, or
a debugging session. The checker does **not** use it: it compares per class through
`allowed_numbers()`, `allowed_identifiers()` and `allowed_statuses()`, which is stricter,
because numbers have to compare as `Decimal` rather than as strings.

`allowed_entities()` is the fourth helper and the odd one out — it is what layer 2 does
*not* check but has to recognise. See the entity row in layer 2's table below.

### Layer 2 — post-hoc verification (`GroundingChecker`)

Runs on the **rendered reply**, unconditionally, regardless of which client produced it.

```python
GroundingChecker.extract(reply: str, facts: FactSet) -> list[Literal]
GroundingChecker.verify(reply: str, facts: FactSet, template: Template) -> GroundingResult
```

`extract` takes the facts because two kinds of span are masked out before scanning, and
`literals_checked` in the trace has to be the count of what was really checked — so the
two cannot take different views of the reply (TRACEABILITY.md).

`extract` pulls every factual token out of the finished text; `verify` checks each
against the `FactSet` and `template.TEMPLATE_SAFE_LITERALS`, and returns both the
`literals` it checked and one `Violation` per unsourced one. The orchestrator records
`len(checked.literals)` as `literals_checked` and hands off on non-empty
`checked.violations` — the guardrail reports, the orchestrator decides. Returning both
from one call is what keeps the reply from being scanned twice by two passes that are
only guaranteed to agree by nothing at all.

| Class | Extraction | Normalisation |
|---|---|---|
| Numbers | `\d+(?:[.,]\d+)?` | parsed to `Decimal`, so `42.10`, `42,10` and `42.1` compare equal |
| Identifiers | fixture id patterns (`inv_`, `sess_`, `u_`) — matched and **masked out first**, so the digits inside `inv_204` are not re-read as an amount | exact match |
| Status words | the closed `InvoiceStatus` / `SessionStatus` vocabularies, with the pattern **built from the enum members** rather than retyped | case-folded match against the statuses actually in the `FactSet`; the `Violation` still records the spelling the reply used |
| Sourced entities | not extracted — **masked out first**, like identifiers. Station names and the user's name, from `allowed_entities()` | n/a. A station the tools returned is sourced text: `A1 Norte` is not an ungrounded `1`, and `Completed Street` is not a status word. Only strings the `FactSet` holds are masked, so an invented station's digits are still scanned |

Any literal not accounted for is a `Violation`. Violations fail closed:
`handed_off` / `UNGROUNDED_REPLY`, the reply discarded, the offending literals recorded
in the trace as evidence.

### `TEMPLATE_SAFE_LITERALS`

A template's own static prose may contain numbers — "within 3 business days". Those are
not facts and will never be in a `FactSet`, so each template declares them explicitly.

It is a **numbers** allowlist and nothing else: the set is unioned into the allowed
numbers only, never into the identifiers or the status words. A template that could
whitelist `failed` would be switching off half the check on its own authority.

The allowlist is per-template and small by design. Adding a number to prose means adding
it here, which is a visible, reviewable act rather than a silent erosion of the
guarantee. A global allowlist would defeat the purpose.

### What layer 2 does not catch

Stated plainly, because a guardrail whose limits are undocumented is a guardrail people
over-trust.

- **Sourcing, not truth.** It confirms every literal came from a tool result. It would not
  catch *"your invoice of EUR 42.10 was paid"* when 42.10 is real but the status is
  `failed`. Layer 1 covers this today because status words are enumerated `FactSet`
  values rather than free text. [Roadmap](../../../docs/ROADMAP.md#semantic-grounding).
- **Open-vocabulary entities.** Extraction covers numbers, ids, and closed status
  vocabularies. An invented station name in free prose would not be caught by layer 2;
  neither would an invented currency, which is none of the three shapes. Under `FakeLLM`
  neither can occur, because layer 1 makes them unreachable. This is the main gap a real
  provider would widen. Note the masking above narrows the *false positive* side of this,
  not the false negative: a **sourced** station is no longer re-read as digits, while an
  invented one is scanned exactly as before.
  [Roadmap](../../../docs/ROADMAP.md#entity-coverage-in-grounding-layer-2).
- **Row counts are facts, so small integers are cheap.** `allowed_numbers()` includes the
  number of invoices and of sessions, because a reply may legitimately say "all 3 of your
  invoices". The cost is that for a user with three invoices, a bare `3` anywhere in the
  prose passes. Deliberate, and the reason `TEMPLATE_SAFE_LITERALS` is still needed: it
  keeps the static numbers declared even where a count would have let them through.
- **Multi-part numerals split.** `1,234.56` extracts as `1,234` and `56`, because the
  comma is read as a decimal separator (that is what makes `42,10` work). Both halves then
  fail closed, so nothing unsourced gets through — but the recorded violation would read
  oddly. No fixture amount reaches four figures.

### How it is proved

`test_grounding.py::test_ungrounded_literal_is_caught` builds a real `Template` — the
production dataclass, real field selection, real safe-literal set — with one amount
doctored into its prose, renders through it, and asserts the `Violation`. A control test
renders the same template undoctored and asserts it is clean, so the failure is the
injected amount and not something incidental in the prose.

Its other half is
`test_an_ungrounded_literal_withholds_the_reply_and_hands_the_ticket_off`, which runs that
same doctored template through the real orchestrator and asserts what a customer would
have received: `reply is None`, `handed_off` / `UNGROUNDED_REPLY`, the literal in the
`grounding_check` step and in the `final_decision` detail. Catching the literal and
sending the reply anyway would pass the first half on its own. A second control replies
honestly through the same path, so a pipeline that handed off every ticket could not pass
by accident. Together they are the evidence the unforgivable bug cannot ship.

---

## Structure

```
guardrails/
  __init__.py
  factset.py       FactSet + InvoiceFact / SessionFact, projection from observations,
                   allowed_literals() and the per-class allowed_* helpers
  grounding.py     GroundingChecker, Literal, GroundingResult, the extraction rules
  limits.py        MAX_ITERATIONS + max_iterations()
  GUARDRAILS.md    this file
```

Two types this package produces are defined outside it, so that `domain` does not end up
importing a component: `HandoffReason` and `LiteralClass` are in `enums.py`, and
`Violation` is in `tracing/models.py` with the trace step that carries it
([ADR 0011](../../../docs/adr/0011-shared-vocabulary-below-the-components.md)).
