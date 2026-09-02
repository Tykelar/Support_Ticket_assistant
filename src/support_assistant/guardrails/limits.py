"""The iteration cap value.

`MAX_ITERATIONS` bounds the agentic tool loop. The model sees prior results before choosing
its next step ([ADR 0002](../../../docs/adr/0002-true-agentic-tool-calling-loop.md)), so it
can loop forever; this is what stands between that and an unbounded run.

This module owns only the number. The loop that enforces it is the orchestrator's, which
imports this value rather than redefining it.

Default 5: `FakeLLM` terminates in at most three iterations, so 5 leaves headroom for a
fourth tool and is low enough that a runaway loop is cheap.
"""

import os

DEFAULT_MAX_ITERATIONS = 5


def max_iterations() -> int:
    """`MAX_ITERATIONS` from the environment, or the default. Raises `ValueError` for
    anything that is not a positive integer: a misconfigured cap should fail at startup
    rather than silently disable the guardrail."""
    raw = os.environ.get("MAX_ITERATIONS")
    if raw is None:
        return DEFAULT_MAX_ITERATIONS
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"MAX_ITERATIONS must be an integer, got {raw!r}") from None
    if value < 1:
        raise ValueError(f"MAX_ITERATIONS must be >= 1, got {value}")
    return value


MAX_ITERATIONS = max_iterations()
"""Resolved once at import. Tests call `max_iterations()` directly with a patched
environment; the orchestrator reads this constant."""
