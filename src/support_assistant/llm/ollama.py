"""`OllamaLLM` -- the optional real `LLMClient`, behind the identical interface as `FakeLLM`.

Off by default. Selected by `LLM_PROVIDER=ollama` (see [`provider.py`](provider.py)); a
clean clone never needs a model server to run the service or pass the tests. It needs the
`ollama` extra ([`pyproject.toml`](../../../pyproject.toml) -- `httpx`, which `[dev]` also
pulls in), which is why `provider.py` imports this module **lazily**: nothing drags
`httpx` onto the default `fake` path.

It talks to a local Ollama server in **JSON mode** and validates the reply straight onto
the domain types -- `TypeAdapter(Step)` for `decide_next_step`, `Classification` for
`classify_intent`. Design and the fail-closed contract are in
[ADR 0015](../../../docs/adr/0015-json-mode-over-the-tool-calling-api.md) and
[LLM.md](LLM.md).

This module may import only `domain`, `enums` and `httpx` (ARCHITECTURE.md section 3;
`tests/test_layering.py` parses the imports and fails on anything else). It therefore
cannot read `tools/registry` -- the tool catalogue it shows the model is the hand-written
`_TOOL_CATALOGUE` below, and the registry stays the enforcement point one layer down.
"""

import json

import httpx
from pydantic import TypeAdapter, ValidationError

from support_assistant.domain import (
    Classification,
    HandoffReason,
    Observation,
    ReplyTemplate,
    Step,
    Ticket,
)

DEFAULT_MODEL = "llama3.1"
"""The model asked for when `OLLAMA_MODEL` is unset. Any model Ollama can run and that
follows a JSON instruction works; the name is configuration, not architecture."""

DEFAULT_TIMEOUT = 30.0
"""Seconds per `/api/chat` call. A model server that hangs fails closed on this rather
than holding the pipeline open. A bare ceiling only -- an adaptive timeout and a circuit
breaker are roadmap (LLM.md)."""

_ENDPOINT = "/api/chat"

_STEP = TypeAdapter(Step)
"""One adapter for the whole decision union, discriminated on `decision`."""

_TOOL_CATALOGUE = (
    "get_user(user_id): the ticket user's profile -- name, language, plan.\n"
    "get_invoices(user_id): the user's invoices, newest first.\n"
    "get_charging_sessions(user_id): the user's charging sessions, newest first."
)
"""Hand-maintained: `llm/` may not import `tools/registry` (ARCHITECTURE.md section 3), so
a fourth tool is a second edit here. Every call is still validated by the registry, so
drift costs a `TOOL_ERROR` handoff, not a wrong reply (LLM.md, ROADMAP)."""

_CLASSIFY_SYSTEM = (
    "You classify EV-charging customer support tickets. Reply with a JSON object "
    '{"intent": "..."} and nothing else. The intent is exactly one of: '
    "billing_question (payments, invoices, refunds, charges); "
    "charging_session_problem (a charging session or station that failed or misbehaved); "
    "unknown (anything else, or a ticket genuinely about both). When in doubt answer "
    "unknown -- a wrong guess is worse than a handoff to a human."
)

_DECIDE_SYSTEM = (
    "You drive a bounded tool-calling loop that answers one support ticket. On each turn "
    "reply with exactly one JSON object, one of:\n"
    '{"decision": "tool_call", "tool": "<name>", "args": {"user_id": "<id>"}} -- gather '
    "more data;\n"
    '{"decision": "reply", "template": "<name>"} -- finish with a templated reply;\n'
    '{"decision": "handoff", "reason": "<REASON>"} -- give up, a human takes the ticket.\n\n'
    f"Tools:\n{_TOOL_CATALOGUE}\n\n"
    "Reply templates (name only -- the system fills them from the gathered data): "
    f"{', '.join(t.value for t in ReplyTemplate)}.\n"
    f"Handoff reasons: {', '.join(r.value for r in HandoffReason)}.\n\n"
    "Always fetch the user before replying -- the reply is addressed by name. Then fetch "
    "the data the ticket's intent needs. Name a reply template only once the gathered "
    "data supports it; otherwise hand off."
)


class OllamaProtocolError(RuntimeError):
    """The model server did not return a well-formed answer -- a transport or HTTP
    failure, non-JSON content, or JSON that does not match `Step` / `Intent`.

    The orchestrator's catch-all turns this into a `TOOL_ERROR` handoff (ADR 0005), so a
    broken or absent model server behaves like any other tool failure: the run stays
    bounded and is handed off, never left in `processing`.
    """


class OllamaLLM:
    """A real model behind the two-method `LLMClient` protocol."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """`base_url` is explicit, like `SqliteTicketRepository`'s `path` -- the provider
        factory supplies the configured one. `transport` is the seam `tests/test_ollama.py`
        uses to stand in for a live server; production leaves it `None`. Nothing here opens
        a connection.
        """
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self._transport = transport

    # -- the protocol ------------------------------------------------------------------

    def classify_intent(self, ticket: Ticket) -> Classification:
        """One of `Intent`'s values, with no evidence: `matched_keywords` is meaningless
        for a real model and an empty tuple says so honestly (ADR 0012).

        A well-formed `"unknown"` is returned as-is -- the orchestrator hands it off as
        `UNSUPPORTED_INTENT`. A value outside the enum is an `OllamaProtocolError`.
        """
        answer = self._chat(
            [
                {"role": "system", "content": _CLASSIFY_SYSTEM},
                {"role": "user", "content": _ticket_text(ticket)},
            ]
        )
        try:
            return Classification.model_validate({"intent": answer.get("intent")})
        except ValidationError as exc:
            raise OllamaProtocolError(
                f"model returned a classification that does not validate: {exc}"
            ) from exc

    def decide_next_step(self, ticket: Ticket, history: list[Observation]) -> Step:
        """Map the model's reply onto `ToolCall | Reply | Handoff` directly (that is why
        the return type *is* the union). A `Reply` names one of the five templates only;
        projecting the `FactSet` and rendering stay the orchestrator's job (LLM.md).
        """
        answer = self._chat(
            [
                {"role": "system", "content": _DECIDE_SYSTEM},
                {"role": "user", "content": _decide_prompt(ticket, history)},
            ]
        )
        try:
            return _STEP.validate_python(answer)
        except ValidationError as exc:
            raise OllamaProtocolError(
                f"model returned a decision that does not validate: {exc}"
            ) from exc

    # -- HTTP ------------------------------------------------------------------------

    def _chat(self, messages: list[dict[str, str]]) -> dict:
        """One `/api/chat` round trip in JSON mode; returns the parsed `message.content`
        object.

        Every way this can fail to hand back a well-formed object -- a transport or timeout
        error, a non-2xx status, a body that is not JSON, a missing `message.content`,
        content that is not JSON despite JSON mode, or a JSON value that is not an object
        -- is one `OllamaProtocolError`. The orchestrator maps them all to a single
        `TOOL_ERROR` handoff (ADR 0015), so one exception with a message that names the
        cause is the whole contract; a type per cause would only be collapsed again.
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "format": "json",
            "stream": False,
        }
        try:
            with httpx.Client(
                base_url=self.base_url, timeout=self.timeout, transport=self._transport
            ) as client:
                response = client.post(_ENDPOINT, json=payload)
            response.raise_for_status()
            parsed = json.loads(response.json()["message"]["content"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise OllamaProtocolError(
                f"chat call to {self.base_url} returned no well-formed answer: {exc!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise OllamaProtocolError(
                f"model returned a JSON {type(parsed).__name__}, not an object"
            )
        return parsed


def _ticket_text(ticket: Ticket) -> str:
    return f"Subject: {ticket.subject}\n\n{ticket.body}"


def _decide_prompt(ticket: Ticket, history: list[Observation]) -> str:
    lines = [f"User id: {ticket.user_id}", "", _ticket_text(ticket), ""]
    if not history:
        lines.append("No tools have run yet.")
    else:
        lines.append("Tool results so far:")
        for obs in history:
            records = json.dumps([r.model_dump(mode="json") for r in obs.result.records])
            lines.append(f"- {obs.step.tool}: {records}")
    lines += ["", "Reply with the next JSON decision."]
    return "\n".join(lines)
