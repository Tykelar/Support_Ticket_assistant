# LLM

One protocol, two implementations. The deterministic fake is the **default**; a real model
is an optional bonus behind the identical interface.

**Why it is shaped this way:** [ADR 0006](../../../docs/adr/0006-fake-first-llm-behind-a-client-protocol.md)
(the protocol and the fake) and [ADR 0002](../../../docs/adr/0002-true-agentic-tool-calling-loop.md)
(why `decide_next_step` is a loop rather than a plan).

---

## The protocol

```python
class LLMClient(Protocol):
    def classify_intent(self, ticket: Ticket) -> Intent: ...

    def decide_next_step(
        self, ticket: Ticket, history: list[Observation]
    ) -> ToolCall | Reply | Handoff: ...
```

`ToolCall`, `Reply`, `Handoff` (discriminated on `decision`) and `Observation` live in
[`domain.py`](../../../ARCHITECTURE.md), not in this package: `guardrails/` consumes
`Observation` for `FactSet.from_observations` and ARCHITECTURE.md §3 forbids it importing
`llm/`. Same reason `ToolResult` sits there.

`classify_intent` returns the `Intent` only. The `matched_keywords` that the
`intent_classified` trace step carries are not on this interface — for a real model
"matched keywords" is meaningless. How the orchestrator gets that evidence out of a
classification is a phase-7 (pipeline) question; today nothing calls this in anger.

Two things about this signature carry weight.

**The return type is a discriminated union, not a string.** A real provider's tool-call
response maps onto it directly, so swapping implementations is a swap and not a redesign.
It also makes "the model decided to give up" (`Handoff`) a first-class outcome the
pipeline handles, rather than an error it has to infer.

**`history` is a parameter.** The client sees the results of previous tool calls before
choosing the next action. That is the whole difference between an agentic loop and a
fixed plan, and it is what makes the iteration cap guard something real.

---

## `FakeLLM` — the default

Deterministic. No clock, no randomness, no network. The same ticket always produces the
same trace, byte for byte.

### Intent classification

Case-insensitive keyword matching over `subject + body`.

| Intent | Keywords |
|---|---|
| `billing_question` | invoice, bill, billing, payment, paid, charge, charged, refund, receipt, cost, price, euro |
| `charging_session_problem` | charging, charger, station, session, kwh, plug, connector, stopped, interrupted |
| `unknown` | anything that matches neither |

Scored by number of distinct keyword hits; the higher score wins.

**A tie resolves to `unknown`**, which means a handoff. A ticket that talks equally about
billing and charging is genuinely ambiguous, and guessing between two templates is
exactly the kind of confident-and-wrong behaviour the system is built to avoid. Failing
closed on ambiguity is the same instinct as ADR 0005, applied to classification.

A support agent sees *why* a ticket was categorised as it was — not just that it was —
from the `matched_keywords` on the `intent_classified` trace step (produced by the
pipeline, see the note under "The protocol").

### Step decision

A small state machine over the observation history. `FakeLLM` re-derives the intent from
the ticket (it holds no state between calls); given that and what has already been
gathered:

```
1. no get_user observation yet          -> ToolCall(get_user, {user_id})
2. intent == billing_question
     and no get_invoices observation    -> ToolCall(get_invoices, {user_id})
3. intent == charging_session_problem
     and no get_charging_sessions obs.  -> ToolCall(get_charging_sessions, {user_id})
4. everything the intent needs is present -> Reply(template)
```

`Reply` names the template only. Projecting the `FactSet` from history and rendering are
the orchestrator's step, not the model's (PIPELINE.md); the fake picks the template from
the statuses in the gathered records — `billing_failed` if any invoice failed, else
`billing_pending` if any is pending, else `billing_all_paid`; for charging, from the most
recent session. Being asked to decide for an `unknown` intent is a contract violation
(the pipeline hands those off first), so `decide_next_step` raises rather than guess.

The tool names in steps 1–3 are checked against `registry.registered()` before the call
is built, so the registry stays the one place tool names are defined — adding a fourth
tool is an entry there plus an intent→tool mapping, not a hunt for literals. This is the
one sanctioned `llm/ → tools/` read (see `registry.registered()`'s docstring).

`get_user` comes first for every intent because the reply is addressed by name, and the
name is a fact that must be sourced like any other.

The fake never returns `Handoff` from `decide_next_step` — it always has a next action or
is finished. `Handoff` is in the union because a real model needs it, and because the
pipeline must handle it either way. The stub that tests the iteration cap exploits this
by returning `ToolCall` forever.

Note that the fake terminates in at most three iterations, comfortably inside the cap of
5. The cap exists for the implementations that do not behave.

### Reply templates

`FakeLLM` picks the template; `templates.py` turns it into prose.

| Template | Chosen when | Interpolates (all from the `FactSet`) | `TEMPLATE_SAFE_LITERALS` |
|---|---|---|---|
| `billing_all_paid` | every invoice is `paid` | name | — |
| `billing_failed` | at least one invoice is `failed` | name, first failed invoice id / amount / currency | — |
| `billing_pending` | at least one `pending`, none `failed` | name, first pending invoice id / amount / currency | `{"3"}` |
| `session_completed` | the most recent session is `completed` | name, station, kWh, cost of `sessions[0]` | — |
| `session_interrupted` | the most recent session is `interrupted` or `failed` | name, station, **status**, kWh, cost of `sessions[0]` | — |

```python
spec_for(template: ReplyTemplate) -> Template   # the spec: prose, fields, safe literals
Template.render(facts: FactSet) -> str          # the reply text
render(template: ReplyTemplate, facts) -> str   # the lookup in front of it
```

The orchestrator resolves the spec once, renders from it, and hands the rendered text, the
same `FactSet`, and that same spec to `GroundingChecker.verify` — rendering and checking
against different templates would compare a reply against the wrong safe list.

`render` raises `ValueError`, naming the fact it wanted, when the `FactSet` cannot fill the
template — no user name, or no invoice or session of the kind the template speaks about.
`FakeLLM` cannot ask for that, since it picks the template from these very records; a real
model can name any of the five, and the orchestrator's catch-all turns the refusal into a
`TOOL_ERROR` handoff with a readable detail rather than a bare `StopIteration`.

Templates interpolate **only** from the `FactSet`. There is no code path from a template
to a value that no tool returned — that is grounding layer 1
([ADR 0004](../../../docs/adr/0004-two-layer-grounding-enforcement.md)).
`session_interrupted` interpolates the session's own status word, so it stays grounded
whether the session is `interrupted` or `failed`.

Any number a template states in its own static prose ("within 3 business days") must be
declared in that template's `TEMPLATE_SAFE_LITERALS`, or the post-hoc grounding checker
will flag it and hand the ticket off. This is a deliberate speed bump: it makes adding
unsourced numbers to prose an explicit, reviewable act. The only one today is the `"3"` in
`billing_pending`.

`templates.py` imports `FactSet` under `TYPE_CHECKING` only: `render` needs its shape for
type-checking but duck-types at runtime, so there is no direct `llm/ → guardrails/` import
and the two packages meet in the orchestrator (ARCHITECTURE.md §3). To be precise about
what that buys: importing `llm.templates` still pulls `guardrails.handoff` and
`guardrails.grounding` in transitively, because `domain` names `HandoffReason` and
`Violation`. The invariant being kept is the *direct* dependency between components, which
is what makes each testable on its own — not module-graph isolation, which `domain` has
never given anyone.

---

## `OllamaLLM` — optional bonus

The same two methods against a local model over HTTP.

- Selected by `LLM_PROVIDER=ollama`; **off by default**.
- A clean clone never needs a model server to run the service or pass the tests.
- Deleting this module would not touch the pipeline. That is the point of the protocol.

It is bounded by the same guardrails as the fake — same iteration cap, same handoff
rules, and critically the same post-hoc grounding check. Grounding layer 2 exists
precisely because layer 1's structural guarantee does not survive free-text generation.

Wired but unpolished: prompt quality is explicitly not what the brief is grading, and
core requirements come first. What production-shaping it would need — timeouts, a
circuit breaker, response schema validation, and a golden-file evaluation set — is in
[the roadmap](../../../docs/ROADMAP.md#hardening-the-real-llm-client).

---

## Structure

```
llm/
  __init__.py    empty, like every component package
  protocol.py    LLMClient (the ToolCall | Reply | Handoff union and Observation it
                 names are in domain.py -- see "The protocol")
  fake.py        FakeLLM: keyword rules + step state machine
  templates.py   the 5 reply bodies, render(), spec_for(), TEMPLATE_SAFE_LITERALS
  ollama.py      OllamaLLM (optional, off by default)                    (phase 11)
  LLM.md         this file
```

---

## Non-goals

**Multilingual replies.** Profiles carry `language` and the fixtures use `pt`, `en`, `fr`.
Replies are English only. Templating in three languages triples the template surface —
and the `TEMPLATE_SAFE_LITERALS` surface with it — while exercising no part of the system
under evaluation. The field is read and recorded in the trace; routing on it is a template
change, not an architecture change. Reasoning in ADR 0006, method in
[the roadmap](../../../docs/ROADMAP.md#replies-in-the-users-language).

**Good classification.** Keyword matching is brittle and is not defended as otherwise. It
is a deterministic stand-in so the loop, the guardrails, and the failure handling can be
evaluated without a model in the way.

**Prompt engineering.** The brief says it is deliberately not evaluating this.
