"""Provider selection -- `LLM_PROVIDER` chooses the `LLMClient` `create_app` runs with.

`build_llm()` is the factory, the same shape as `storage.sqlite.database_path()` and
`guardrails.limits.max_iterations()`: read the environment, fall back to the default, and
fail loudly on a value that is not a real choice.

Offline: `OllamaLLM.__init__` opens no connection, so `LLM_PROVIDER=ollama` is safe to
build here without a model server.
"""

import pytest
from fastapi.testclient import TestClient

from support_assistant.api.app import create_app
from support_assistant.llm.fake import FakeLLM
from support_assistant.llm.ollama import OllamaLLM
from support_assistant.llm.provider import build_llm

_ENV_VARS = ("LLM_PROVIDER", "OLLAMA_BASE_URL", "OLLAMA_MODEL")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_the_default_provider_is_the_fake() -> None:
    assert isinstance(build_llm(), FakeLLM)


def test_fake_is_selected_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    assert isinstance(build_llm(), FakeLLM)


def test_ollama_is_selected_and_carries_its_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://model-host:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "mistral")

    llm = build_llm()

    assert isinstance(llm, OllamaLLM)
    assert llm.base_url == "http://model-host:11434"
    assert llm.model == "mistral"


def test_an_unknown_provider_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "gpt5")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        build_llm()


def test_create_app_builds_the_configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    app = create_app()
    with TestClient(app):
        assert isinstance(app.state.llm, OllamaLLM)


def test_an_injected_client_wins_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    injected = FakeLLM()
    app = create_app(llm=injected)
    with TestClient(app):
        assert app.state.llm is injected
