# ADR 0007 — Component packages with co-located documentation

**Status:** Accepted · 2026-08-31

## Context

This system has a small number of parts with genuinely distinct responsibilities — the
API surface, the orchestration loop, the model client, the tools, the guardrails, the
trace, storage, and observability. Each needs documenting: what it is, what its contract
is, how it fails, and why it is built that way.

The default place for that would be a `docs/` directory with one file per part. The
problem with it is drift: a document three directories away from the code it describes
goes stale without anything visibly breaking, and a reviewer reading `pipeline.py` has no
reason to discover that `docs/PIPELINE.md` exists.

## Decision

Each component is a package under `src/support_assistant/`, and its documentation lives
inside that package next to the code it describes.

```
src/support_assistant/
  api/            + API.md
  pipeline/       + PIPELINE.md
  llm/            + LLM.md
  tools/          + TOOLS.md
  guardrails/     + GUARDRAILS.md
  tracing/        + TRACEABILITY.md
  storage/        + STORAGE.md
  observability/  + OBSERVABILITY.md
tests/            + TESTS.md
deploy/           + PACKAGING.md
```

Cross-cutting documents stay central: `ARCHITECTURE.md` at the root describes the
pipeline end to end, and `docs/` holds the ADRs, the glossary, and the original brief.

The division of labour between them: a component doc says **what this part does and how
to use it**; an ADR says **why it is this way**. The *why* is written once, in the ADR,
and linked. Component docs do not restate it.

## Consequences

- A change to a component and the change to its documentation land in the same directory,
  usually the same commit and the same diff hunk. Drift becomes visible in review.
- Anyone reading a package sees its contract without leaving the directory.
- Standard `src/` layout is preserved, so imports and packaging stay idiomatic.
- Markdown inside a Python package is slightly unusual, and the wheel build must not ship
  it — handled by the `hatch` packaging config.
- Documentation is spread across ten locations. `ARCHITECTURE.md` and the README index
  them so the set is still navigable from one place.

## Alternatives considered

**All docs under `docs/`.** The most conventional layout and familiar to any Python
reviewer. Rejected for the drift and discoverability reasons above.

**Component directories at the repository root.** Makes the parts obvious on first `ls`.
Rejected: ten top-level directories, and non-idiomatic imports.
