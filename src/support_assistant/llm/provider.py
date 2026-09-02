"""`build_llm()` -- `LLM_PROVIDER` picks the `LLMClient` the running service uses.

The same shape as `storage.sqlite.database_path()` and `guardrails.limits.max_iterations()`:
a function, not a constant read at import, so a container or a test sets the environment
without import order deciding the answer. `create_app` calls it for the `llm` collaborator
when none is injected; an injected client still wins.

`OllamaLLM` is imported **inside** the `ollama` branch, not at module top: that is what
keeps `httpx` (and the `ollama` extra) off the default `fake` path, so a clean production
install without `[dev]`/`[ollama]` still boots.
"""

import os

from support_assistant.llm.fake import FakeLLM
from support_assistant.llm.protocol import LLMClient

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "llama3.1"


def build_llm() -> LLMClient:
    """The client named by `LLM_PROVIDER` (default `fake`), case-insensitive.

    `fake` (or unset) -> `FakeLLM`. `ollama` -> `OllamaLLM` reading `OLLAMA_BASE_URL` and
    `OLLAMA_MODEL`. Any other value raises `ValueError` -- a misconfigured provider fails
    loudly at startup rather than silently falling back, the same instinct as
    `max_iterations()`.
    """
    provider = os.environ.get("LLM_PROVIDER", "fake").strip().lower()
    if provider in ("", "fake"):
        return FakeLLM()
    if provider == "ollama":
        from support_assistant.llm.ollama import OllamaLLM

        return OllamaLLM(
            base_url=os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL),
            model=os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
        )
    raise ValueError(f"LLM_PROVIDER must be 'fake' or 'ollama', got {provider!r}")
