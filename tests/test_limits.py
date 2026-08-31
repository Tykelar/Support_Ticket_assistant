"""`guardrails.limits` -- the iteration cap value.

The `for ... else` that enforces the cap is the orchestrator's (phase 7); this module
only owns the number and its validation. `MAX_ITERATIONS` defaults to 5 and is
environment-configurable (GUARDRAILS.md section 1, ARCHITECTURE.md section 4).
"""

import pytest

from support_assistant.guardrails.limits import DEFAULT_MAX_ITERATIONS, max_iterations


def test_the_default_is_five() -> None:
    assert DEFAULT_MAX_ITERATIONS == 5


def test_it_reads_the_default_when_the_env_var_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAX_ITERATIONS", raising=False)
    assert max_iterations() == 5


def test_a_valid_override_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_ITERATIONS", "8")
    assert max_iterations() == 8


@pytest.mark.parametrize("bad", ["0", "-1", "nan", "", "3.5"])
def test_a_bad_override_is_rejected(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("MAX_ITERATIONS", bad)
    with pytest.raises(ValueError):
        max_iterations()
