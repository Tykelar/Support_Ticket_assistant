"""`build_llm()` -- `LLM_PROVIDER` picks the `LLMClient` the running service uses.

A function, not a constant read at import, like `storage.sqlite.database_path()`.
`create_app` calls it when no client is injected; an injected one still wins.

`OllamaLLM` is imported **inside** the `ollama` branch, which is what keeps `httpx` off the
default `fake` path so a clean install without the extras still boots.
"""

import os

from support_assistant.llm.fake import FakeLLM
from support_assistant.llm.protocol import LLMClient

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
"""Where a stock local Ollama listens. Here rather than on `OllamaLLM`, whose `base_url` is
a required argument."""


def build_llm() -> LLMClient:
    """The client named by `LLM_PROVIDER` (default `fake`), case-insensitive.

    `ollama` reads `OLLAMA_BASE_URL` and `OLLAMA_MODEL`. Any other value raises: a
    misconfigured provider fails at startup rather than silently falling back.
    """
    provider = os.environ.get("LLM_PROVIDER", "fake").strip().lower()
    if provider in ("", "fake"):
        return FakeLLM()
    if provider == "ollama":
        from support_assistant.llm.ollama import DEFAULT_MODEL, OllamaLLM

        return OllamaLLM(
            base_url=os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL),
            model=os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL),
        )
    raise ValueError(f"LLM_PROVIDER must be 'fake' or 'ollama', got {provider!r}")
