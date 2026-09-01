# ADR 0012 — A classification carries the evidence for itself

**Status:** Accepted · 2026-09-01

Supersedes the `classify_intent` signature in
[ADR 0006](0006-fake-first-llm-behind-a-client-protocol.md). Everything else in that ADR
stands.

## Context

Two documents wanted different things from the same call.

[TRACEABILITY.md](../../src/support_assistant/tracing/TRACEABILITY.md) puts
`matched_keywords` on the `intent_classified` step, and names it in the workflow a
support agent follows: step 2 is *"was the ticket even understood correctly?
`matched_keywords` shows the evidence"*. [LLM.md](../../src/support_assistant/llm/LLM.md)
makes the same promise from the other side — an agent sees *why* a ticket was categorised,
not just that it was.

ADR 0006's protocol could not supply it:

```python
def classify_intent(self, ticket: Ticket) -> Intent: ...
```

The orchestrator records the step, holds an `LLMClient`, and has no way to ask what the
classification was based on. LLM.md said so explicitly and deferred the question to the
pipeline phase. The pipeline is now here, and the deferral has to resolve one way or the
other: either the evidence reaches the trace, or both documents are describing a field
that is always empty.

The reason it cannot be reconstructed outside the classifier is the whole point. The
keyword vocabularies are `FakeLLM`'s own; re-running them in the orchestrator would put a
copy of one component's rules inside another, and it would be a copy that could drift and
still look right — the trace would keep showing plausible evidence for a decision it no
longer described.

## Decision

`classify_intent` returns a `Classification`, not an `Intent`.

```python
class Classification(BaseModel):
    intent: Intent
    matched_keywords: tuple[str, ...] = ()
```

It lives in `domain.py`, beside the `ToolCall | Reply | Handoff` union and for the same
reason: `pipeline/` and `llm/` both name it and neither may import the other
([ARCHITECTURE.md](../../ARCHITECTURE.md) §3).

**`matched_keywords` defaults to empty.** A real provider has no keyword hits to report,
and a protocol that *obliged* every implementation to produce evidence would get invented
evidence — the failure mode this system exists to prevent, wearing a smaller costume. An
empty tuple is an honest "no evidence to give"; the trace then shows an intent with
nothing behind it, which is the truth about that classification.

**The field keeps the trace's name.** `matched_keywords` is what
`intent_classified` carries, so there is one term rather than a neutral one on the
protocol translated to a specific one at the recording site. The name is admittedly
keyword-flavoured for a model that does not match keywords; that cost is smaller than a
second vocabulary for one value, and `CONTEXT.md` already discourages *evidence* as a term
of its own.

**`FakeLLM` reports the winning category's hits only**, sorted and de-duplicated. Sorted
because determinism is the fake's contract (ADR 0006) — the same ticket produces the same
trace byte for byte. Winner-only because listing the loser's hits would make the step
argue for a classification the fake did not make; an `unknown` outcome — nothing matched,
or a tie — carries no evidence at all, which is exactly right, since there is no evidence
*for* handing off beyond the absence of a winner.

## Consequences

- The `intent_classified` step carries real evidence, so TRACEABILITY.md's reading
  workflow and LLM.md's promise are both true rather than aspirational.
- The orchestrator holds no copy of any classifier's rules. It records what it was told.
- `decide_next_step` is untouched. It re-derives the intent internally via
  `classify_intent(ticket).intent`, which is one attribute access, not a redesign.
- One more type in the domain vocabulary. It is a two-field record, and it is the return
  type of a protocol method rather than a concept a reader has to hold — CONTEXT.md gains
  an entry so it is defined once.
- A real provider that *can* explain itself has somewhere to put it. A model returning
  the phrase it keyed on fills the same field; nothing about the trace shape changes.

## Alternatives considered

**A second, `FakeLLM`-only method the orchestrator special-cases.** Keeps ADR 0006's
signature untouched. Rejected outright: the orchestrator holds an `LLMClient`, so it would
have to type-check the implementation it was handed and branch on it — reaching around
the protocol at the one place the protocol exists to protect. The next implementation
either grows the same method or silently loses the evidence.

**Record `matched_keywords: []` for every client.** No protocol change, no ADR, no new
type. Rejected because it makes the field decorative: TRACEABILITY.md's step 2 would point
at a list that is always empty, and the honest version of this option is to delete the
field and amend both documents. Deleting it is worse — the evidence for a classification
is precisely what an agent asking "why did the AI say this?" wants second, after the
outcome.

**Have the orchestrator re-run the keyword match.** Cheapest to write. Rejected: it copies
one component's rules into another, and a copy that drifts still produces confident,
plausible, wrong evidence in an audit record. It would also be flatly wrong under any
implementation that does not classify by keywords.

**A free-text `reason: str` instead of a keyword list.** More natural for a real model.
Rejected for now: it is not machine-readable, `matched_keywords` already has a documented
JSON shape and a test, and a string invites the model to explain itself at length in a
field the trace shows a human under time pressure. A provider that wants prose can put it
in a follow-up ADR.
