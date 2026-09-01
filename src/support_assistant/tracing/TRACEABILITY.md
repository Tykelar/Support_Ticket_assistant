# Traceability

> A support agent must be able to answer "why did the AI say this?" from
> `GET /tickets/{id}` alone.

That sentence is the whole specification for this package. Not a log file, not a
dashboard, not a debugger — one API call.

---

## The trace

An ordered list of typed steps, persisted with the ticket and returned by `GET`. Every
step has `seq` (1-based, monotonic), `ts`, and `type`; the rest of the fields depend on
the type.

`ts` comes from an injected `Clock` rather than `datetime.now()`
([ADR 0008](../../../docs/adr/0008-injected-clock-with-advancing-test-double.md)). In
production that is real UTC time; in tests it is a frozen clock advancing a fixed 10ms
tick, so traces stay byte-for-byte reproducible while durations remain real enough to
assert on. Timestamps must increase with `seq`, and a contract test enforces it — which is
the check a constant clock would silently make impossible.

| Step | Emitted | Carries |
|---|---|---|
| `intent_classified` | once, before the loop | `intent`, `matched_keywords` |
| `llm_decision` | once per iteration | `iteration`, `decision` (`tool_call` / `reply` / `handoff`), `tool` if any |
| `tool_call` | per tool invocation | `tool`, `args` |
| `tool_result` | per tool invocation | `tool`, `ok`, `summary`, or `error` |
| `grounding_check` | once, if a reply was drafted | `passed`, `literals_checked`, `violations` |
| `final_decision` | exactly once, always last | `outcome`, `reason`, `detail` |

**`final_decision` is always present in a terminal state, and there is exactly one.**
The orchestrator's `_decide` returns an outcome and is never handed the repository;
`run_pipeline` records this step and persists it in the single `finalise` call below the
catch-all. A terminal ticket without a final decision — or with two — is therefore
structurally impossible rather than merely unlikely
([ADR 0013](../../../docs/adr/0013-one-write-outside-the-catch-all.md),
[PIPELINE.md](../pipeline/PIPELINE.md)).

`llm_decision` is recorded separately from `tool_call` on purpose. One says what the model
*chose*; the other says what the system *did*. Collapsing them would hide the case where
those diverge — a rejected tool name, a validation failure before execution — which is
precisely the case worth seeing.

---

## Why results are summarised

`tool_result` records a **summary**, not the payload:

```json
{ "seq": 7, "ts": "2026-08-31T10:14:02.113Z",
  "type": "tool_result", "tool": "get_invoices", "ok": true,
  "summary": { "count": 3, "statuses": { "paid": 2, "failed": 1 },
               "referenced": ["inv_204", "inv_203", "inv_202"] } }
```

Three reasons, in order of importance:

1. **The brief asks for it** — "each tool call with its arguments and (summarized)
   result".
2. **Readability.** The trace exists to be read by a human under time pressure. A full
   invoice dump per call buries the reasoning in data the agent can look up elsewhere.
3. **Data minimisation.** The trace is retained for audit and is served over the API.
   Copying every field of every record into it multiplies the exposure of customer data
   for no gain.

### The summarisation rule

Enough to explain the reply, never more:

- **counts** — how many rows came back;
- **distribution over enumerated fields** — the statuses and their frequencies;
- **the identifiers the result returned** — so a reader can go from a sentence in the
  reply to the exact source record.

What is deliberately excluded: full field-by-field records.

`summarise(result)` in [summarise.py](summarise.py) produces this, one rule per tool. The
`referenced` list holds every identifier the call returned. Narrowing it to just the
ones the rendered reply cites stays a deferred refinement now that the orchestrator
exists: doing it means rewriting a step the recorder has already stamped, which is a
mutating method on `TraceRecorder` bought for a data-minimisation gain the `count` and
`statuses` fields do not share. The full list is a safe superset — a reader can still
trace any statement in the reply to a record — and the narrowing is on
[the roadmap](../../../docs/ROADMAP.md#narrowing-a-traces-referenced-ids). The status
distribution is
emitted in enum-declaration order rather than row order, and the persist step preserves
that ordering rather than sorting it ([STORAGE.md](../storage/STORAGE.md)).

**The tension worth naming:** summarisation is lossy, and a lossy audit record can fail to
explain an outcome. The rule above is chosen so the *decision-relevant* facts always
survive — the `referenced` list is what guarantees a reader can trace any statement in the
reply back to a specific record. Per-record field detail is the only thing dropped.

---

## Failure detail

Failure steps carry more than success steps, because a failure is what someone will
actually be reading:

```json
{ "seq": 4, "type": "tool_result", "tool": "get_invoices", "ok": false,
  "error": { "type": "NoDataAvailable", "message": "user u_004 has no invoices" } }

{ "seq": 5, "type": "final_decision", "outcome": "handed_off",
  "reason": "DATA_NOT_FOUND",
  "detail": "get_invoices found no records for u_004" }
```

`Violation` is defined in `tracing/models.py` rather than in `guardrails/`: it is the
payload of this step, and a guardrail type named here would put that whole package
underneath `domain` ([ADR 0011](../../../docs/adr/0011-shared-vocabulary-below-the-components.md)).
Its `class` key is a `LiteralClass` — `number`, `identifier` or `status`.

Grounding violations record the specific offending literals — the evidence for why a
reply was withheld:

```json
{ "seq": 9, "type": "grounding_check", "passed": false, "literals_checked": 4,
  "violations": [ { "literal": "99.00", "class": "number",
                    "reason": "not present in FactSet or TEMPLATE_SAFE_LITERALS" } ] }
```

Stack traces go to the structured log, not the trace. The trace answers *why the AI said
this*; a traceback answers *why the code broke*. Different audiences, different places.

**`ToolError` is not `ToolExecutionError`.** The first is the model above — a failure as
recorded in a `tool_result` step. The second is one of the three exceptions a tool raises
([TOOLS.md](../tools/TOOLS.md)). The similarity is unfortunate but the names are right:
`ToolError` records *any* exception that reached the call site, `NoDataAvailable` and
`UserNotFound` included, so naming it after one of the three would be actively
misleading. Which component writes the step is settled in
[ADR 0010](../../../docs/adr/0010-the-orchestrator-records-tool-steps.md).

---

## Recording

The recorder is constructed with a `Clock` and stamps every step it appends:

```python
class TraceRecorder:
    def __init__(self, clock: Clock) -> None
    def intent_classified(self, intent, matched_keywords) -> None
    def llm_decision(self, iteration, decision, tool=None) -> None
    def tool_call(self, tool, args) -> None
    def tool_result(self, tool, summary=None, error=None) -> None
    def grounding_check(self, literals_checked, violations) -> None
    def final_decision(self, outcome, reason=None, detail=None) -> None
```

`llm_decision` takes the decision (`tool_call` / `reply` / `handoff`) and, for a tool
call, the tool name — not the LLM's step object. The `ToolCall` / `Reply` / `Handoff`
types arrive with `llm/` in a later phase and nothing in `tracing/` may depend on `llm/`
([ARCHITECTURE.md](../../../ARCHITECTURE.md) §3); the orchestrator unpacks its own `step`
at the call site. Three coherence rules are enforced by the models, not left to the
caller: `tool_result`'s `ok` is derived from whether an `error` is present,
`grounding_check`'s `passed` from whether `violations` is empty, and `llm_decision` names
a `tool` exactly when the decision is `tool_call`.

Steps accumulate in memory during the run and are persisted with the terminal state in one
transaction, so a ticket is never observed with a terminal status but a truncated trace
([STORAGE.md](../storage/STORAGE.md)).

**The trade-off:** a process that dies mid-run loses the partial trace along with the
ticket, which stays in `processing` (ADR 0001's known limitation). Incremental persistence
would preserve the partial trace at the cost of a write per step and the atomicity above.
Given that a stranded ticket needs a reaper either way
([roadmap](../../../docs/ROADMAP.md#durable-work-and-a-reaper)), the atomic write is the better trade here — and the structured log retains a per-step record for exactly this case
([OBSERVABILITY.md](../observability/OBSERVABILITY.md)).

`grounding_check` takes the count as well as the violations because `passed` follows from
whether the list is empty but `literals_checked` does not, and it is what distinguishes a
check that passed from one that had nothing to look at: `passed: true, literals_checked: 0`
is a reply the checker never really inspected, and a bare `passed: true` would hide it.
`verify` returns the literals it checked alongside the violations, and the orchestrator
records `len(checked.literals)` — so the count comes from the pass that did the checking,
rather than from a second reading that is only guaranteed to agree by nothing at all.
That is also why `extract` takes the `FactSet` as well as the reply: it masks sourced
spans before scanning, and a count taken without them would not describe the same check.

The recorder is injected, not global, so tests assert on the recorded steps directly.

---

## Reading a trace

The intended workflow when an agent asks "why did the AI say this?":

1. `final_decision` — what happened and why, in one step.
2. `intent_classified` — was the ticket even understood correctly? `matched_keywords`
   shows the evidence.
3. `tool_call` / `tool_result` pairs — what data the reply was built from.
4. `grounding_check` — for a withheld reply, exactly which literal was unsourced.

`GET /tickets/{id}` returns all of it. No log access, no database query, no second call.

---

## Structure

```
tracing/
  __init__.py
  models.py        TraceStep types, discriminated by `type`
  recorder.py      TraceRecorder
  summarise.py     tool-result summarisation rules, per tool
  TRACEABILITY.md  this file
```
