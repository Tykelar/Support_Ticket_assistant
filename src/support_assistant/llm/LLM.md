# LLM

One protocol, two implementations. The deterministic fake is the **default**; a real model
is an optional bonus behind the identical interface.

Why it is shaped this way:
[ADR 0006](../../../docs/adr/0006-fake-first-llm-behind-a-client-protocol.md) (the protocol
and the fake) and
[ADR 0002](../../../docs/adr/0002-true-agentic-tool-calling-loop.md) (why
`decide_next_step` is a loop rather than a plan).

---

## The protocol

```python
class LLMClient(Protocol):
    def classify_intent(self, ticket: Ticket) -> Classification: ...

    def decide_next_step(
        self, ticket: Ticket, history: list[Observation]
    ) -> ToolCall | Reply | Handoff: ...
```

The types it exchanges live in [`domain.py`](../domain.py), not here: `guardrails/`
consumes `Observation` and ARCHITECTURE.md §3 forbids it importing `llm/`.

Three things about this signature carry weight.

**The return type is a discriminated union, not a string.** A real provider's response maps
onto it directly, so swapping implementations is a swap rather than a redesign. It also
makes "the model decided to give up" (`Handoff`) a first-class outcome the pipeline
handles, rather than an error it has to infer.

**`history` is a parameter.** The client sees the results of previous tool calls before
choosing the next action. That is the difference between an agentic loop and a fixed plan,
and it is what makes the iteration cap guard something real.

**`classify_intent` returns the evidence with the intent.** Only the classifier knows
`matched_keywords`, and re-deriving them in the orchestrator would put a copy of this
package's rules in `pipeline/`
([ADR 0012](../../../docs/adr/0012-classification-carries-its-own-evidence.md)). It
defaults to empty, so a real model — for which "matched keywords" is meaningless — is not
obliged to invent any.

---

## `FakeLLM` — the default

Deterministic. No clock, no randomness, no network. The same ticket always produces the
same trace, byte for byte.

### Intent classification

Case-insensitive keyword matching over `subject + body`, scored by distinct hits.

| Intent | Keywords |
|---|---|
| `billing_question` | invoice, bill, billing, payment, paid, charge, charged, refund, receipt, cost, price, euro |
| `charging_session_problem` | charging, charger, station, session, kwh, plug, connector, stopped, interrupted |
| `unknown` | anything that matches neither |

The evidence is the **winning** category's hits, sorted and de-duplicated: listing the
loser's would make the trace argue for a classification the fake did not make, and an
`unknown` outcome carries none at all.

**A tie resolves to `unknown`**, which means a handoff. A ticket that talks equally about
billing and charging is genuinely ambiguous, and guessing between two templates is the
confident-and-wrong behaviour the system is built to avoid.

### Step decision

A state machine over the observation history. The fake holds no state between calls, so it
re-derives the intent from the ticket:

```
1. no get_user observation yet            -> ToolCall(get_user, {user_id})
2. billing, no get_invoices observation   -> ToolCall(get_invoices, {user_id})
3. charging, no sessions observation      -> ToolCall(get_charging_sessions, {user_id})
4. everything the intent needs is present -> Reply(template)
```

`get_user` comes first for every intent because the reply is addressed by name, and the
name is a fact that must be sourced like any other.

`Reply` names the template only — projecting the `FactSet` and rendering are the
orchestrator's step (PIPELINE.md). The fake picks it from the gathered statuses:
`billing_failed` if any invoice failed, else `billing_pending` if any is pending, else
`billing_all_paid`; for charging, from the most recent session.

Tool names are checked against `registry.registered()` before the call is built, so the
registry stays the one place tool names are defined. This is the one sanctioned
`llm/ → tools/` read.

The fake never returns `Handoff` — it always has a next action or is finished, and
terminates in at most three iterations. `Handoff` is in the union because a real model
needs it. Being asked to decide for an `unknown` intent is a contract violation (the
pipeline hands those off first), so `decide_next_step` raises rather than guess.

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
```

The orchestrator resolves the spec once, renders from it, and hands the text, the same
`FactSet` and that same spec to `GroundingChecker.verify`. Rendering and checking against
different templates would compare a reply against the wrong safe list, which is why there
is deliberately no `render(template, facts)` shortcut beside `spec_for`.

`Template.render` raises `ValueError` naming the fact it wanted when the `FactSet` cannot
fill the template. `FakeLLM` cannot ask for that, since it picks the template from these
records; a real model can name any of the five, and the catch-all turns the refusal into a
`TOOL_ERROR` handoff with a readable detail rather than a bare `StopIteration`.

Templates interpolate **only** from the `FactSet` — grounding layer 1
([ADR 0004](../../../docs/adr/0004-two-layer-grounding-enforcement.md)). Any number in a
template's own static prose must be declared in its `TEMPLATE_SAFE_LITERALS`, or the
checker will flag it: a deliberate speed bump that makes adding unsourced numbers a
reviewable act. Numbers only, so a template cannot whitelist `failed` (GUARDRAILS.md).

`templates.py` imports `FactSet` under `TYPE_CHECKING` only, so there is no runtime
`llm/ → guardrails/` import and the two packages meet in the orchestrator.
`tests/test_layering.py` parses the imports and skips `TYPE_CHECKING` blocks, so the
arrangement stays legal and a runtime import would not.

---

## `OllamaLLM` — optional bonus

The same two methods against a local Ollama server over HTTP (`httpx`, an
[`ollama` extra](../../../pyproject.toml)).

- Selected by `LLM_PROVIDER=ollama`; **off by default**.
- A clean clone never needs a model server to run the service or pass the tests.
- Deleting this module would not touch the pipeline. That is the point of the protocol.

### How it talks to the model

`POST {base_url}/api/chat` in **JSON mode** — `"format": "json"`, `"stream": false` — and
the reply in `message.content` is validated straight onto the domain types. JSON mode
rather than the tool-calling API because `Reply` and `Handoff` are decisions, not tools,
and `llm/` cannot import the registry to build a `tools` array anyway
([ADR 0015](../../../docs/adr/0015-json-mode-over-the-tool-calling-api.md)).

`base_url` is a constructor argument. [`provider.py`](provider.py) reads `LLM_PROVIDER` /
`OLLAMA_BASE_URL` / `OLLAMA_MODEL` and builds one; `create_app(llm=...)` still wins when a
test injects a client, and an unrecognised `LLM_PROVIDER` raises rather than falling
through.

### Failure is closed and uniform

Any failure to get a well-formed answer — transport or timeout error, non-2xx response,
content that is not JSON, or JSON that does not validate — raises `OllamaProtocolError`,
which the catch-all turns into a `TOOL_ERROR` handoff. A well-formed answer naming an
unregistered tool fails the same way through `registry.run`. A well-formed
`{"intent": "unknown"}` is **not** an error — it is an `UNSUPPORTED_INTENT` handoff.

Bounded by the same guardrails as the fake: same cap, same handoff rules, and critically
the same post-hoc grounding check, which exists precisely because layer 1 does not survive
free-text generation.

### Wired but unpolished

Prompt quality is not what the brief is grading. Each of these is in
[the roadmap](../../../docs/ROADMAP.md#hardening-the-real-llm-client):

- **Fixed HTTP timeout, no circuit breaker** — a hanging server fails closed on the
  timeout, but nothing sheds load after repeated failures.
- **The tool catalogue in the prompt is hand-maintained**, since `llm/` may not import
  `tools/`. The registry stays the enforcement point, so drift costs a `TOOL_ERROR`
  handoff, not a wrong reply.
- **No token accounting per ticket**, and **no golden-file evaluation set** — the piece
  that matters most, and the one that only makes sense once a real model is in use.
- **One `httpx.Client` per call** — connection pooling is deferred.

---

## Structure

```
llm/
  __init__.py    empty, like every component package
  protocol.py    LLMClient (the union and Observation it names are in domain.py)
  fake.py        FakeLLM: keyword rules + step state machine
  templates.py   the 5 reply bodies, spec_for(), Template.render(), TEMPLATE_SAFE_LITERALS
  ollama.py      OllamaLLM (optional, off by default) + OllamaProtocolError
  provider.py    build_llm(): LLM_PROVIDER picks the client create_app runs with
  LLM.md         this file
```

---

## Non-goals

**Multilingual replies.** Profiles carry `language` and the fixtures use `pt`, `en`, `fr`,
but replies are English only. Three languages would triple the template and safe-literal
surface while exercising no part of the system under evaluation. The field is read and
traced; routing on it is a template change, not an architecture change
([roadmap](../../../docs/ROADMAP.md#replies-in-the-users-language)).

**Good classification.** Keyword matching is brittle and is not defended as otherwise. It
is a deterministic stand-in so the loop, the guardrails and the failure handling can be
evaluated without a model in the way.

**Prompt engineering.** The brief says it is deliberately not evaluating this.
