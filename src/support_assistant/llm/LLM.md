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

The matched keywords are recorded in the `intent_classified` trace step, so a support
agent can see *why* a ticket was categorised as it was — not just that it was.

### Step decision

A small state machine over the observation history. Given the intent and what has already
been gathered:

```
1. no get_user observation yet          -> ToolCall(get_user, {user_id})
2. intent == billing_question
     and no get_invoices observation    -> ToolCall(get_invoices, {user_id})
3. intent == charging_session_problem
     and no get_charging_sessions obs.  -> ToolCall(get_charging_sessions, {user_id})
4. everything the intent needs is present -> Reply(template, facts)
```

`get_user` comes first for every intent because the reply is addressed by name, and the
name is a fact that must be sourced like any other.

The fake never returns `Handoff` from `decide_next_step` — it always has a next action or
is finished. `Handoff` is in the union because a real model needs it, and because the
pipeline must handle it either way. The stub that tests the iteration cap exploits this
by returning `ToolCall` forever.

Note that the fake terminates in at most three iterations, comfortably inside the cap of
5. The cap exists for the implementations that do not behave.

### Reply templates

| Template | Chosen when |
|---|---|
| `billing_all_paid` | every invoice is `paid` |
| `billing_failed` | at least one invoice is `failed` |
| `billing_pending` | at least one `pending`, none `failed` |
| `session_completed` | the most recent session is `completed` |
| `session_interrupted` | the most recent session is `interrupted` or `failed` |

Templates interpolate **only** from the `FactSet`. There is no code path from a template
to a value that no tool returned — that is grounding layer 1
([ADR 0004](../../../docs/adr/0004-two-layer-grounding-enforcement.md)).

Any number a template states in its own static prose ("within 3 business days") must be
declared in that template's `TEMPLATE_SAFE_LITERALS`, or the post-hoc grounding checker
will flag it and hand the ticket off. This is a deliberate speed bump: it makes adding
unsourced numbers to prose an explicit, reviewable act.

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
  __init__.py
  protocol.py    LLMClient, ToolCall, Reply, Handoff, Observation
  fake.py        FakeLLM: keyword rules + step state machine
  templates.py   reply templates + TEMPLATE_SAFE_LITERALS per template
  ollama.py      OllamaLLM (optional, off by default)
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
