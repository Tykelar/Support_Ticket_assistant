# Traceability

> A support agent must be able to answer "why did the AI say this?" from
> `GET /tickets/{id}` alone.

That sentence is the whole specification for this package. Not a log file, not a dashboard,
not a debugger — one API call.

---

## The trace

An ordered list of typed steps, persisted with the ticket and returned by `GET`. Every step
has `seq` (1-based, monotonic), `ts` and `type`; the rest depends on the type.

`ts` comes from an injected `Clock`
([ADR 0008](../../../docs/adr/0008-injected-clock-with-advancing-test-double.md)). In
production that is real UTC; in tests a frozen clock advancing 10ms per reading, so traces
are reproducible while durations stay real enough to assert on. Timestamps must increase
with `seq`, and a contract test enforces it — the check a constant clock would silently
make impossible.

| Step | Emitted | Carries |
|---|---|---|
| `intent_classified` | once, before the loop | `intent`, `matched_keywords` |
| `llm_decision` | once per iteration | `iteration`, `decision`, `tool` if any |
| `tool_call` | per tool invocation | `tool`, `args` |
| `tool_result` | per tool invocation | `tool`, `ok`, `summary`, or `error` |
| `grounding_check` | once, if a reply was drafted | `passed`, `literals_checked`, `violations` |
| `final_decision` | exactly once, always last | `outcome`, `reason`, `detail` |

**`final_decision` is always present in a terminal state, and there is exactly one.**
`_decide` returns an outcome and is never handed the repository; `run_pipeline` records the
step and persists it in the single `finalise` call below the catch-all, so a terminal ticket
without one — or with two — is structurally impossible
([ADR 0013](../../../docs/adr/0013-one-write-outside-the-catch-all.md)).

`llm_decision` is separate from `tool_call` on purpose: one says what the model *chose*, the
other what the system *did*. Collapsing them would hide the case where they diverge.

Every `tool_call` is followed by a `tool_result`, success or failure — the loop records one
from inside the `try`, so a raising tool or an unsummarisable result cannot leave an
unpaired call in the trace.

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

1. **The brief asks for it** — "each tool call with its arguments and (summarized) result".
2. **Readability.** The trace is read by a human under time pressure; a full invoice dump
   per call buries the reasoning in data the agent can look up elsewhere.
3. **Data minimisation.** The trace is retained for audit and served over the API.

The rule is *enough to explain the reply, never more*: counts, the distribution over
enumerated fields, and the identifiers returned — so a reader can go from a sentence in the
reply to the exact source record. Full field-by-field records are excluded.

`summarise(result)` holds one rule per tool. `referenced` lists every identifier the call
returned, a safe superset of the ones the reply cites; narrowing it means rewriting a step
the recorder already stamped, so it is
[deferred](../../../docs/ROADMAP.md#narrowing-a-traces-referenced-ids). The status
distribution is emitted in enum-declaration order, and the persist step preserves that
rather than sorting ([STORAGE.md](../storage/STORAGE.md)).

**The tension worth naming:** summarisation is lossy, and a lossy audit record can fail to
explain an outcome. The rule is chosen so the decision-relevant facts always survive —
`referenced` is what guarantees a reader can trace any statement back to a record. Only
per-record field detail is dropped.

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

Grounding violations record the specific offending literals — the evidence for why a reply
was withheld:

```json
{ "seq": 9, "type": "grounding_check", "passed": false, "literals_checked": 4,
  "violations": [ { "literal": "99.00", "class": "number",
                    "reason": "not present in FactSet or TEMPLATE_SAFE_LITERALS" } ] }
```

`Violation` is defined in `tracing/models.py` rather than `guardrails/`: it is the payload
of this step, and a guardrail type named here would put that package underneath `domain`
([ADR 0011](../../../docs/adr/0011-shared-vocabulary-below-the-components.md)). Its `class`
key is a `LiteralClass` — `number`, `identifier` or `status`.

Stack traces go to the structured log, not the trace. The trace answers *why the AI said
this*; a traceback answers *why the code broke*.

**`ToolError` is not `ToolExecutionError`.** The first is the model above — a failure as
recorded in a step. The second is one of the three exceptions a tool raises. The similarity
is unfortunate but the names are right: `ToolError` records *any* exception that reached the
call site, so naming it after one of the three would be misleading.

---

## Recording

```python
class TraceRecorder:
    def __init__(self, clock: Clock, *, on_step: Callable[[TraceStep], None] | None = None) -> None
    def intent_classified(self, intent, matched_keywords) -> None
    def llm_decision(self, iteration, decision, tool=None) -> None
    def tool_call(self, tool, args) -> None
    def tool_result(self, tool, summary=None, error=None) -> None
    def grounding_check(self, literals_checked, violations) -> None
    def final_decision(self, outcome, reason=None, detail=None) -> None
```

`on_step`, when given, is called with each step as it is recorded — the seam the
orchestrator wires `log_step` into, so the log survives a process death that loses the
not-yet-persisted trace. Injected rather than imported, which keeps `tracing/` ignorant of
the logger ([OBSERVABILITY.md](../observability/OBSERVABILITY.md)).

`llm_decision` takes the decision and tool name, not the LLM's step object, because nothing
in `tracing/` may depend on `llm/` ([ARCHITECTURE.md](../../../ARCHITECTURE.md) §3).

Three coherence rules are enforced by the models rather than left to the caller:
`tool_result`'s `ok` is derived from whether an `error` is present, `grounding_check`'s
`passed` from whether `violations` is empty, and `llm_decision` names a `tool` exactly when
the decision is `tool_call`.

`literals_checked` is recorded separately from `passed` because it distinguishes a check
that passed from one that had nothing to look at — `passed: true, literals_checked: 0` is a
reply the checker never inspected. `verify` returns the literals it checked alongside the
violations, so the count comes from the pass that did the checking.

Steps accumulate in memory and are persisted with the terminal state in one transaction, so
a ticket is never observed with a terminal status but a truncated trace.

**The trade-off:** a process that dies mid-run loses the partial trace along with the
ticket, which stays in `processing`. Incremental persistence would preserve it at the cost
of a write per step and that atomicity. Since a stranded ticket needs a
[reaper](../../../docs/ROADMAP.md#durable-work-and-a-reaper) either way, the atomic write is
the better trade — and the structured log retains a per-step record for exactly this case.

---

## Reading a trace

The intended workflow when an agent asks "why did the AI say this?":

1. `final_decision` — what happened and why, in one step.
2. `intent_classified` — was the ticket even understood correctly? `matched_keywords` shows
   the evidence.
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
