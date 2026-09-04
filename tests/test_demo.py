"""The demo scenarios, driven through the real pipeline, and the page that displays them.

The scenarios exist to be *shown to someone*, which is exactly the kind of artefact that
rots: a fixture gains a row, a keyword list changes, and the ticket labelled "a payment
that failed" quietly starts handing off instead. So every scenario declares the outcome it
claims to demonstrate and this file makes it earn that claim through the production wiring
-- real SQLite, real orchestrator, `FakeLLM` (`conftest.py`).

That makes this the one file that walks all four handoff reasons reachable through the API
in a single run, and it pins the fixture-to-path map that
[TOOLS.md](../src/support_assistant/tools/TOOLS.md) documents in prose.

The last two tests guard the page rather than the pipeline: that it is served, and that
every endpoint its JavaScript calls actually exists on the app. Same shape of guard as
`test_layering.py` (parses imports rather than trusting the diagram) and `test_docs.py`
(parses the doc) -- a fetch to a route that was renamed is invisible until someone opens a
browser.
"""

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from support_assistant.api.schemas import CreateTicketRequest
from support_assistant.demo import STATIC_DIR, load_scenarios
from support_assistant.enums import HandoffReason, ReplyTemplate, TicketStatus

SCENARIOS = load_scenarios()

REACHABLE_REASONS = {
    HandoffReason.USER_NOT_FOUND,
    HandoffReason.DATA_NOT_FOUND,
    HandoffReason.UNSUPPORTED_INTENT,
    HandoffReason.TOOL_ERROR,
}
"""The reasons a ticket posted to the API can actually produce under `FakeLLM`."""

UNREACHABLE_THROUGH_THE_API = {
    HandoffReason.ITERATION_CAP_EXCEEDED,
    HandoffReason.UNGROUNDED_REPLY,
}
"""The two no seeded ticket can demonstrate, named here rather than merely absent.

`FakeLLM` never returns `Handoff` and terminates in three iterations, and grounding layer 1
renders only from the `FactSet`, so neither fires through the front door
([ADR 0004](../docs/adr/0004-two-layer-grounding-enforcement.md)). Reaching them takes an
injected misbehaving client, which `test_iteration_cap.py` and `test_grounding.py` do.
DEMO.md says so on the page; this pins the claim so it cannot go stale.
"""


def _ids(scenarios: list[dict[str, Any]]) -> list[str]:
    return [scenario["id"] for scenario in scenarios]


REPLIED = [s for s in SCENARIOS if s["expect"]["status"] == TicketStatus.REPLIED]


# --------------------------------------------------------------------------------------
# The scenarios, against a real run
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_ids(SCENARIOS))
def test_every_scenario_is_a_request_the_api_would_accept(scenario: dict[str, Any]) -> None:
    """Validated against the real edge schema, so a scenario cannot ship a body over the
    length bound or a field the API forbids."""
    CreateTicketRequest.model_validate(
        {key: scenario[key] for key in ("user_id", "subject", "body")}
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_ids(SCENARIOS))
def test_every_scenario_reaches_the_outcome_it_claims_to_demonstrate(
    scenario: dict[str, Any], submit: Any
) -> None:
    ticket = submit(scenario["user_id"], scenario["subject"], scenario["body"])
    expected = scenario["expect"]
    assert ticket["status"] == expected["status"], scenario["label"]
    assert ticket["handoff_reason"] == expected["handoff_reason"], scenario["label"]


@pytest.mark.parametrize("scenario", REPLIED, ids=_ids(REPLIED))
def test_every_replied_scenario_cites_the_records_it_promises(
    scenario: dict[str, Any], submit: Any
) -> None:
    """The declared `reply_mentions` are the point of the scenario -- "a payment that
    failed" is only a demonstration if the reply actually names the failed invoice. Each
    snippet is a value from the fixtures, so this also pins the five templates apart from
    one another."""
    ticket = submit(scenario["user_id"], scenario["subject"], scenario["body"])
    for mention in scenario["expect"]["reply_mentions"]:
        assert mention in ticket["reply"], f"{scenario['id']} does not mention {mention!r}"


def test_a_handed_off_scenario_promises_no_reply_text() -> None:
    """A handoff sends the customer nothing (ADR 0005), so a scenario declaring one has no
    business claiming the reply mentions anything."""
    for scenario in SCENARIOS:
        if scenario["expect"]["status"] == TicketStatus.HANDED_OFF:
            assert scenario["expect"]["reply_mentions"] == [], scenario["id"]


# --------------------------------------------------------------------------------------
# Coverage: what the seeded set is supposed to be for
# --------------------------------------------------------------------------------------


def test_the_scenarios_cover_every_handoff_reason_the_api_can_produce() -> None:
    declared = {
        s["expect"]["handoff_reason"] for s in SCENARIOS if s["expect"]["handoff_reason"]
    }
    assert declared == {reason.value for reason in REACHABLE_REASONS}


def test_the_two_lists_of_reasons_together_are_the_whole_enum() -> None:
    """Otherwise a seventh `HandoffReason` could be added and silently belong to neither
    list -- undemonstrated and undocumented at the same time."""
    assert set(HandoffReason) == REACHABLE_REASONS | UNREACHABLE_THROUGH_THE_API
    assert not REACHABLE_REASONS & UNREACHABLE_THROUGH_THE_API


def test_the_replied_scenarios_exercise_all_five_templates(submit: Any) -> None:
    """Counted by distinct reply prose rather than by declaring a template name: the API
    never serves the template, so the only honest evidence that five different templates
    ran is five different replies."""
    replies = {
        submit(s["user_id"], s["subject"], s["body"])["reply"] for s in REPLIED
    }
    assert len(replies) == len(ReplyTemplate)


# --------------------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/ui/", "text/html"),
        ("/ui/app.js", "javascript"),
        ("/ui/style.css", "text/css"),
        ("/ui/scenarios.json", "application/json"),
    ],
)
def test_the_page_and_its_assets_are_served(
    e2e_client: TestClient, path: str, content_type: str
) -> None:
    response = e2e_client.get(path)
    assert response.status_code == 200, path
    assert content_type in response.headers["content-type"]


_SCRIPT_ELEMENT = re.compile(r"""\$\(\s*["']([^"']+)["']\s*\)""")
_PAGE_ELEMENT = re.compile(r"""\bid=["']([^"']+)["']""")


def test_the_page_holds_every_element_the_script_reaches_for() -> None:
    """`document.getElementById` returns `null` for a typo and the page half-works in
    silence -- no console error until something touches the result. Cheap to pin: the
    script's `$("...")` calls are exactly the ids the page must carry."""
    wanted = set(_SCRIPT_ELEMENT.findall((STATIC_DIR / "app.js").read_text(encoding="utf-8")))
    present = set(_PAGE_ELEMENT.findall((STATIC_DIR / "index.html").read_text(encoding="utf-8")))
    assert wanted, "no element lookups found in app.js -- this guard has stopped working"
    assert wanted <= present, f"app.js reaches for ids the page does not have: {wanted - present}"


_FETCH = re.compile(r"""fetch\(\s*[`'"]([^`'"]+)""")
"""A fetch call's first argument, whichever quote style it uses. Template placeholders
survive the capture because `${...}` contains none of the three quote characters."""

_PLACEHOLDER = re.compile(r"\$\{[^}]*\}|\{[^}]*\}")
"""`${id}` in the JavaScript and `{ticket_id}` in the route, both reduced to the same
token so a path with a parameter compares equal to the route that serves it."""


def test_the_page_calls_only_endpoints_that_exist(e2e_client: TestClient) -> None:
    """A renamed route is invisible until someone opens a browser, and this page is the
    thing a reviewer opens.

    Compared against the published OpenAPI paths rather than `app.routes`: the schema is
    the contract API.md documents, and walking the route objects means tracking how the
    installed FastAPI happens to nest an included router.
    """
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    called = {
        _PLACEHOLDER.sub("{}", path)
        for path in _FETCH.findall(script)
        if path.startswith("/")
    }
    served = {
        _PLACEHOLDER.sub("{}", path)
        for path in e2e_client.get("/openapi.json").json()["paths"]
    }
    assert called, "no absolute fetch paths found in app.js -- this guard has stopped working"
    assert called <= served, f"app.js calls endpoints the app does not serve: {called - served}"
