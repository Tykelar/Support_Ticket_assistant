# ADR 0006 — Fake-first LLM behind a client protocol

**Status:** Accepted · 2026-08-31

## Context

The brief asks for an LLM client interface with a deterministic, rule-based fake behind
it, and says wiring a real provider is an optional bonus "never at the expense of the
core requirements". Keyword matching is explicitly fine.

The interesting constraint is that the fake is not a stand-in for the real thing — it is
the *default* implementation, and it is what every test runs against. So the interface
has to be shaped by what a real provider needs, or swapping one in later becomes a
rewrite; and the fake has to be genuinely deterministic, or the test suite inherits the
flakiness the fake exists to avoid.

## Decision

One protocol, two implementations.

```python
class LLMClient(Protocol):
    def classify_intent(self, ticket: Ticket) -> Intent: ...
    def decide_next_step(self, ticket: Ticket, history: list[Observation]) -> ToolCall | Reply | Handoff: ...
```

- **`FakeLLM` (default).** Keyword rules over the ticket subject and body pick the
  intent; a small state machine over the observation history picks the next step;
  replies are rendered from templates fed by the `FactSet` (ADR 0004). No clock, no
  randomness, no network — the same ticket always produces the same trace.
- **`OllamaLLM` (optional bonus).** The same two methods against a local model. Selected
  by `LLM_PROVIDER=ollama`; **off by default**, so a clean clone never needs a model
  server to run or to pass its tests.

The return type is a discriminated union rather than free text. A real provider's
tool-call response maps onto it directly, which is what keeps the swap honest.

## Consequences

- Every test is deterministic and offline. No network in CI, no snapshot churn.
- The bonus is genuinely additive: deleting `OllamaLLM` would not touch the pipeline.
- The union return type means the pipeline handles a model that decides to give up
  (`Handoff`) as a first-class outcome rather than an error.
- The fake carries some real logic — intent rules and a step state machine — which needs
  its own tests. It is a component, not a stub.
- Keyword matching is brittle by nature. It is not defended as good classification; it is
  a deterministic stand-in that lets the surrounding system be evaluated. Real
  classification quality is a model concern, and the brief explicitly is not grading it.

### Note (2026-09-01): `classify_intent` now returns a `Classification`

The signature in the Decision above returned a bare `Intent`. It could not supply the
`matched_keywords` the `intent_classified` trace step carries, which left LLM.md and
TRACEABILITY.md both promising evidence no interface could deliver.
[ADR 0012](0012-classification-carries-its-own-evidence.md) supersedes that one line:
`classify_intent(ticket) -> Classification`, carrying the intent and the evidence for it,
with the evidence optional so a real provider is not obliged to invent any. Nothing else
here changes -- two methods, a discriminated union from `decide_next_step`, the fake as
the default.

## Non-goal: multilingual replies

User profiles carry a `language` field, and the fixtures use several. Replies are
generated in **English only**.

This is a deliberate scope cut, not an oversight. Templating in multiple languages
multiplies the template surface — and therefore the `TEMPLATE_SAFE_LITERALS` surface from
ADR 0004 — without exercising any part of the system under evaluation. The field is read
and recorded in the trace so the pipeline demonstrably has it; routing on it is a
template change, not an architecture change.

## Alternatives considered

**A single `generate(prompt) -> str` interface.** Closer to a raw completion API.
Rejected: it pushes tool-call parsing into the pipeline and makes the fake's structured
decisions awkward to express.

**Real provider as the default with the fake for tests.** Rejected: it makes a clean
clone depend on a model server, which the brief rules out.
