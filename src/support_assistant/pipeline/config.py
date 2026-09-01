"""The orchestrator's env-backed settings.

`MAX_ITERATIONS` is **imported**, not redefined. The value and its validation belong to
`guardrails/limits.py`; the `for ... else` that enforces it belongs to the orchestrator
(GUARDRAILS.md section 1, PIPELINE.md). Re-reading the environment here would give the
system two sources of truth for one guardrail, and the one a test patched would not
necessarily be the one the loop read.

`DATABASE_PATH` is deliberately absent: it belongs to `storage/`
([STORAGE.md](../storage/STORAGE.md) has the table), and the orchestrator is handed a
repository rather than building one.
"""

from support_assistant.guardrails.limits import MAX_ITERATIONS, max_iterations

__all__ = ["MAX_ITERATIONS", "max_iterations"]
