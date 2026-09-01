"""API.md's worked examples, checked against what the code actually produces.

This is the guard the session that wrote the API.md example should have had. That
example was wrong twice over -- `matched_keywords` unsorted and one short, `updated_at`
missing entirely -- and it shipped anyway because nothing but a human proofreading the
prose would have caught it. The repo already has this shape of guard for other kinds of
drift: `test_layering.py` parses imports instead of trusting the architecture diagram,
`test_clock.py` greps for `datetime.now(` instead of trusting a code-review comment. This
does the same for documentation: parse the fenced JSON blocks out of the doc and check
them against the real request/response cycle they claim to describe.

**The three blocks tell one story.** The `POST /tickets` request example and the
`GET /tickets/{id}` response example are the *same ticket* -- same user, same subject,
same body -- which is what lets this test drive the documented request through the real
pipeline and compare the documented response to what comes back, rather than checking the
two blocks in isolation and missing that they had drifted apart from each other.

What is deliberately *not* compared: the ticket id, the timestamps, and the elided middle
of the reply. Those vary run to run by design (a fresh 128-bit id, a real clock, the
example's own `...`) and pinning them would make this test flake or lie. What must match:
every key present, every value that ADR 0006's deterministic `FakeLLM` actually produces
for this input, and the reply text up to where the example truncates it.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from support_assistant.api.schemas import CreateTicketRequest, TicketAccepted

API_MD = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "support_assistant"
    / "api"
    / "API.md"
)


def _json_block_after(anchor: str) -> Any:
    """The first fenced ` ```json ` block in API.md that follows `anchor`.

    Regex over the raw markdown rather than a doc-parsing library: API.md is the
    ground truth either way, and a fenced code block is unambiguous to find without one.
    """
    text = API_MD.read_text(encoding="utf-8")
    start = text.index(anchor)
    match = re.search(r"```json\n(.*?)\n```", text[start:], re.DOTALL)
    assert match is not None, f"no ```json block found after {anchor!r} in API.md"
    return json.loads(match.group(1))


def _step_keys(step: dict[str, Any]) -> set[str]:
    return set(step)


def _without_timestamp(step: dict[str, Any]) -> dict[str, Any]:
    """A step's content, `ts` aside. `ts` comes from a real clock in both the documented
    example and this test's own run (ADR 0008), so it is the one field that is expected
    to differ and is not part of what this test is checking."""
    return {key: value for key, value in step.items() if key != "ts"}


@pytest.fixture
def request_example() -> dict[str, Any]:
    return _json_block_after("## `POST /tickets`")


@pytest.fixture
def accepted_example() -> dict[str, Any]:
    return _json_block_after("**Response — `202 Accepted`**")


@pytest.fixture
def documented_ticket() -> dict[str, Any]:
    return _json_block_after("## `GET /tickets/{id}`")


@pytest.fixture
def live_ticket(submit: Any, request_example: dict[str, Any]) -> dict[str, Any]:
    """The documented request, actually run, through the real HTTP surface and the real
    pipeline. If this fixture fails to build, the example request itself is unusable."""
    return submit(
        request_example["user_id"], request_example["subject"], request_example["body"]
    )


# --------------------------------------------------------------------------------------
# The POST examples
# --------------------------------------------------------------------------------------


def test_the_documented_request_is_a_valid_request(request_example: dict[str, Any]) -> None:
    CreateTicketRequest.model_validate(request_example)


def test_the_documented_202_matches_the_accepted_shape(accepted_example: dict[str, Any]) -> None:
    accepted = TicketAccepted.model_validate(accepted_example)
    assert accepted.status.value == "processing"


# --------------------------------------------------------------------------------------
# The GET example, against what that exact request really produces
# --------------------------------------------------------------------------------------


def test_the_documented_ticket_has_exactly_the_documented_keys(
    documented_ticket: dict[str, Any], live_ticket: dict[str, Any]
) -> None:
    assert set(documented_ticket) == set(live_ticket)


def test_the_documented_outcome_matches_a_real_run(
    documented_ticket: dict[str, Any], live_ticket: dict[str, Any]
) -> None:
    assert documented_ticket["status"] == live_ticket["status"]
    assert documented_ticket["handoff_reason"] == live_ticket["handoff_reason"]


def test_the_documented_reply_is_a_true_prefix_of_a_real_reply(
    documented_ticket: dict[str, Any], live_ticket: dict[str, Any]
) -> None:
    """The doc elides the reply's middle with a literal `...`. What is left has to be
    real prose the system actually writes, not a hand-composed approximation of it."""
    documented_reply = documented_ticket["reply"]
    assert documented_reply.endswith("...")
    assert live_ticket["reply"].startswith(documented_reply.removesuffix("..."))


def test_the_documented_matched_keywords_are_what_fakellm_really_returns(
    documented_ticket: dict[str, Any], live_ticket: dict[str, Any]
) -> None:
    """The exact defect this test exists to catch: FakeLLM sorts and de-duplicates
    (ADR 0006), so an example composed by hand is the one place that rule is easy to
    forget."""
    documented = next(s for s in documented_ticket["trace"] if s["type"] == "intent_classified")
    live = next(s for s in live_ticket["trace"] if s["type"] == "intent_classified")
    assert documented["matched_keywords"] == live["matched_keywords"]
    assert documented["intent"] == live["intent"]


def test_the_documented_trace_has_the_real_step_sequence(
    documented_ticket: dict[str, Any], live_ticket: dict[str, Any]
) -> None:
    assert [s["type"] for s in documented_ticket["trace"]] == [
        s["type"] for s in live_ticket["trace"]
    ]


def test_each_documented_step_carries_exactly_the_keys_that_step_type_emits(
    documented_ticket: dict[str, Any], live_ticket: dict[str, Any]
) -> None:
    """API.md states the rule ("a step carries only the keys that apply to it") in
    prose; this checks the example actually obeys it, step by step -- a `tool` on a
    `reply` decision, or a stray `null` field, would fail here even though it would not
    fail the shape checks above."""
    for documented_step, live_step in zip(
        documented_ticket["trace"], live_ticket["trace"], strict=True
    ):
        assert _step_keys(documented_step) == _step_keys(live_step), documented_step["type"]


def test_the_documented_tool_calls_and_results_match_a_real_run(
    documented_ticket: dict[str, Any], live_ticket: dict[str, Any]
) -> None:
    for kind in ("tool_call", "tool_result"):
        documented_steps = [
            _without_timestamp(s) for s in documented_ticket["trace"] if s["type"] == kind
        ]
        live_steps = [_without_timestamp(s) for s in live_ticket["trace"] if s["type"] == kind]
        assert documented_steps == live_steps, kind


def test_the_documented_grounding_check_matches_a_real_run(
    documented_ticket: dict[str, Any], live_ticket: dict[str, Any]
) -> None:
    documented = next(s for s in documented_ticket["trace"] if s["type"] == "grounding_check")
    live = next(s for s in live_ticket["trace"] if s["type"] == "grounding_check")
    assert _without_timestamp(documented) == _without_timestamp(live)


def test_the_documented_final_decision_matches_a_real_run(
    documented_ticket: dict[str, Any], live_ticket: dict[str, Any]
) -> None:
    documented = documented_ticket["trace"][-1]
    live = live_ticket["trace"][-1]
    assert documented["type"] == "final_decision" == live["type"]
    assert _without_timestamp(documented) == _without_timestamp(live)
