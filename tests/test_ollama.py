"""`OllamaLLM` -- the optional real client, exercised against a mocked transport.

Reserved by TESTS.md: a **request-shape unit test**. No live model server -- the suite is
deterministic and offline (TESTS.md opening promise), so `httpx.MockTransport` stands in
for Ollama and the assertions are about the request we send and how we parse a canned
response.

What is pinned here:

- the request goes to `POST {base_url}/api/chat` in JSON mode, carrying the ticket text
  and the tool catalogue;
- a canned tool-call response parses to a `ToolCall`, a canned final response to a `Reply`
  naming a `ReplyTemplate`, a canned give-up to a `Handoff`;
- a well-formed `"unknown"` classification is a value, not an error;
- **every way the model server can fail to give a well-formed answer raises
  `OllamaProtocolError`** -- which the orchestrator's catch-all turns into a `TOOL_ERROR`
  handoff (`pipeline/orchestrator.py`). Malformed content and a 5xx both stand for that.
"""

import json
from datetime import UTC, datetime

import httpx
import pytest

from support_assistant.domain import (
    Classification,
    Handoff,
    Intent,
    Reply,
    ReplyTemplate,
    Ticket,
    ToolCall,
    new_ticket_id,
)
from support_assistant.llm.ollama import OllamaLLM, OllamaProtocolError

_NOW = datetime(2026, 8, 31, 10, 14, tzinfo=UTC)
_BASE_URL = "http://ollama.test"


def _ticket(subject: str, body: str, *, user_id: str = "u_002") -> Ticket:
    return Ticket(
        id=new_ticket_id(),
        user_id=user_id,
        subject=subject,
        body=body,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _client(
    content: str, *, status: int = 200, seen: list[httpx.Request] | None = None
) -> OllamaLLM:
    """An `OllamaLLM` whose transport returns one canned `/api/chat` response.

    `content` is the assistant message body Ollama puts in `message.content` -- a JSON
    *string* in JSON mode. `seen`, if given, collects the requests for shape assertions.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": "boom"})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})

    return OllamaLLM(base_url=_BASE_URL, transport=httpx.MockTransport(handler))


def _sent_body(seen: list[httpx.Request]) -> dict:
    assert len(seen) == 1
    return json.loads(seen[0].content.decode())


# --------------------------------------------------------------------------------------
# classify_intent
# --------------------------------------------------------------------------------------


def test_classify_intent_posts_the_ticket_to_the_chat_endpoint_in_json_mode() -> None:
    seen: list[httpx.Request] = []
    llm = _client('{"intent": "billing_question"}', seen=seen)

    result = llm.classify_intent(
        _ticket("My payment failed", "The invoice could not be charged, what happened?")
    )

    assert result == Classification(intent=Intent.BILLING_QUESTION, matched_keywords=())
    assert seen[0].method == "POST"
    assert seen[0].url == httpx.URL(f"{_BASE_URL}/api/chat")
    body = _sent_body(seen)
    assert body["format"] == "json"
    assert body["stream"] is False
    assert body["model"]  # some model name was sent
    conversation = json.dumps(body["messages"])
    assert "My payment failed" in conversation
    assert "could not be charged" in conversation


def test_a_well_formed_unknown_classification_is_a_value_not_an_error() -> None:
    llm = _client('{"intent": "unknown"}')
    # The model answered; it just could not place the ticket. That is UNSUPPORTED_INTENT
    # downstream, not TOOL_ERROR -- so it must not raise.
    assert llm.classify_intent(_ticket("hello", "are you there")).intent is Intent.UNKNOWN


def test_classify_intent_rejects_an_intent_outside_the_enum() -> None:
    llm = _client('{"intent": "please_refund_me"}')
    with pytest.raises(OllamaProtocolError):
        llm.classify_intent(_ticket("s", "b"))


# --------------------------------------------------------------------------------------
# decide_next_step
# --------------------------------------------------------------------------------------


def test_decide_next_step_offers_the_tools_and_templates_and_parses_a_tool_call() -> None:
    seen: list[httpx.Request] = []
    llm = _client(
        '{"decision": "tool_call", "tool": "get_user", "args": {"user_id": "u_002"}}',
        seen=seen,
    )

    step = llm.decide_next_step(_ticket("s", "b", user_id="u_002"), [])

    assert step == ToolCall(tool="get_user", args={"user_id": "u_002"})
    conversation = json.dumps(_sent_body(seen)["messages"])
    assert "get_user" in conversation
    assert "get_invoices" in conversation
    assert "get_charging_sessions" in conversation
    assert ReplyTemplate.BILLING_FAILED.value in conversation


def test_decide_next_step_parses_a_final_reply_naming_a_template() -> None:
    llm = _client('{"decision": "reply", "template": "billing_failed"}')
    step = llm.decide_next_step(_ticket("s", "b"), [])
    assert step == Reply(template=ReplyTemplate.BILLING_FAILED)


def test_decide_next_step_parses_a_handoff() -> None:
    llm = _client('{"decision": "handoff", "reason": "TOOL_ERROR"}')
    step = llm.decide_next_step(_ticket("s", "b"), [])
    assert isinstance(step, Handoff)


def test_decide_next_step_rejects_a_reply_for_an_unknown_template() -> None:
    llm = _client('{"decision": "reply", "template": "give_them_a_discount"}')
    with pytest.raises(OllamaProtocolError):
        llm.decide_next_step(_ticket("s", "b"), [])


# --------------------------------------------------------------------------------------
# every failure to get a well-formed answer is OllamaProtocolError -> TOOL_ERROR
# --------------------------------------------------------------------------------------


def test_malformed_response_content_fails_closed() -> None:
    llm = _client("not json at all, just prose")
    with pytest.raises(OllamaProtocolError):
        llm.decide_next_step(_ticket("s", "b"), [])


def test_a_server_error_fails_closed() -> None:
    llm = _client('{"intent": "billing_question"}', status=503)
    with pytest.raises(OllamaProtocolError):
        llm.classify_intent(_ticket("s", "b"))


def test_a_json_non_object_response_fails_closed() -> None:
    # Valid JSON in JSON mode, but an array, not an object. `classify_intent` would call
    # `.get` on it -- the shape guard must turn that into a closed failure, not an
    # AttributeError that escapes as something other than TOOL_ERROR.
    llm = _client("[]")
    with pytest.raises(OllamaProtocolError):
        llm.classify_intent(_ticket("s", "b"))
