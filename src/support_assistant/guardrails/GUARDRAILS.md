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

**How it is proved.** A stub client that returns `ToolCall` forever must produce a
`handed_off` ticket with `ITERATION_CAP_EXCEEDED` after exactly `MAX_ITERATIONS`
iterations — asserted on the count, not just the outcome, so an off-by-one is caught.

---

## 2. Handoff rules

The brief requires a handoff when the user or requested data doesn't exist, the intent is
outside the supported categories, or any step fails. That maps to six reasons.

| Reason | Trigger | Detected by |
|---|---|---|
| `USER_NOT_FOUND` | no fixture record for the `user_id` | `UserNotFound` from any tool |
| `DATA_NOT_FOUND` | user exists, no rows of the needed kind | `NoDataAvailable` from any tool |
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

### Layer 2 — post-hoc verification (`GroundingChecker`)

Runs on the **rendered reply**, unconditionally, regardless of which client produced it.

```python
GroundingChecker.verify(reply: str, facts: FactSet, template: Template) -> list[Violation]
```

It extracts literals from the finished text and checks each against
`facts.allowed_literals() | template.TEMPLATE_SAFE_LITERALS`:

| Class | Extraction | Normalisation |
|---|---|---|
| Numbers | `\d+(?:[.,]\d+)?` | parsed to `Decimal`, so `42.10`, `42,10` and `42.1` compare equal |
| Identifiers | fixture id patterns (`inv_`, `sess_`, `u_`) | exact match |
| Status words | the closed vocabularies `paid\|pending\|failed`, `completed\|interrupted` | exact match against the statuses actually in the `FactSet` |

Any literal not accounted for is a `Violation`. Violations fail closed:
`handed_off` / `UNGROUNDED_REPLY`, the reply discarded, the offending literals recorded
in the trace as evidence.

### `TEMPLATE_SAFE_LITERALS`

A template's own static prose may contain numbers — "within 3 business days". Those are
not facts and will never be in a `FactSet`, so each template declares them explicitly.

The allowlist is per-template and small by design. Adding a number to prose means adding
it here, which is a visible, reviewable act rather than a silent erosion of the
guarantee. A global allowlist would defeat the purpose.

### What layer 2 does not catch

Stated plainly, because a guardrail whose limits are undocumented is a guardrail people
over-trust.

- **Sourcing, not truth.** It confirms every literal came from a tool result. It would not
  catch *"your invoice of EUR 42.10 was paid"* when 42.10 is real but the status is
  `failed`. Layer 1 covers this today because status words are enumerated `FactSet`
  values rather than free text. Under a real LLM it needs an entailment check against the
  `FactSet` — out of scope, recorded in the README.
- **Open-vocabulary entities.** Extraction covers numbers, ids, and closed status
  vocabularies. An invented station name in free prose would not be caught by layer 2;
  under `FakeLLM` it cannot occur, because layer 1 makes it unreachable. This is the main
  gap a real provider would widen, and closing it means an entity check against the
  `FactSet`'s string values.

### How it is proved

A test renders through a **deliberately doctored template** that injects an amount absent
from the tool results, and asserts the ticket reaches `handed_off` with
`UNGROUNDED_REPLY`. That test is the evidence the unforgivable bug cannot ship.

---

## Structure

```
guardrails/
  __init__.py
  factset.py       FactSet, projection from observations, allowed_literals()
  grounding.py     GroundingChecker, Violation, literal extraction
  handoff.py       HandoffReason enum + typed failures
  limits.py        MAX_ITERATIONS and the cap helper
  GUARDRAILS.md    this file
```
