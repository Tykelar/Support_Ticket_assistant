"""The registry -- the loop's only route to a tool.

Three things it owes, from TOOLS.md and ADR 0010: containment (an unregistered name is a
typed failure, not an AttributeError), argument validation before the tool body runs, and
a uniform `ToolResult` out. What it must *not* do is trace -- it takes no recorder and no
clock, and typed tool failures pass straight through it.
"""

import inspect
from decimal import Decimal

import pytest

from support_assistant.domain import Invoice, ToolResult, User
from support_assistant.tools import registry
from support_assistant.tools.errors import ToolExecutionError, UserNotFound

# --------------------------------------------------------------------------------------
# The happy path -- a tool's return value, wrapped uniformly.
# --------------------------------------------------------------------------------------


def test_run_wraps_a_single_record_in_a_one_element_tool_result() -> None:
    result = registry.run("get_user", {"user_id": "u_001"})
    assert isinstance(result, ToolResult)
    assert result.tool == "get_user"
    assert len(result.records) == 1
    assert isinstance(result.records[0], User)


def test_run_passes_a_collection_through_and_keeps_the_concrete_type() -> None:
    result = registry.run("get_invoices", {"user_id": "u_002"})
    assert result.tool == "get_invoices"
    assert [i.invoice_id for i in result.records] == ["inv_204", "inv_203", "inv_202"]
    # The records must stay Invoice, not be flattened to the FixtureRecord base -- the
    # per-tool rules downstream dispatch on the concrete shape.
    assert all(isinstance(r, Invoice) for r in result.records)
    assert result.records[0].amount == Decimal("42.10")


def test_registered_lists_exactly_the_three_tools() -> None:
    assert set(registry.registered()) == {
        "get_user",
        "get_charging_sessions",
        "get_invoices",
    }


# --------------------------------------------------------------------------------------
# Containment and argument validation -- both fail before any tool body runs.
# --------------------------------------------------------------------------------------


def test_an_unregistered_name_is_a_tool_execution_error() -> None:
    # Not an AttributeError, and not a call to something that was never meant to be a tool.
    with pytest.raises(ToolExecutionError):
        registry.run("get_tariffs", {"user_id": "u_001"})


def test_a_missing_argument_fails_before_the_tool_runs() -> None:
    # If this leaked through, the loader would raise UserNotFound for a None user_id and
    # the failure would look like missing data rather than a bad call.
    with pytest.raises(ToolExecutionError):
        registry.run("get_user", {})


def test_an_empty_argument_is_rejected() -> None:
    with pytest.raises(ToolExecutionError):
        registry.run("get_user", {"user_id": ""})


def test_an_unexpected_argument_is_rejected() -> None:
    with pytest.raises(ToolExecutionError):
        registry.run("get_user", {"user_id": "u_001", "verbose": True})


# --------------------------------------------------------------------------------------
# Typed tool failures pass straight through -- the registry does not convert them.
# --------------------------------------------------------------------------------------


def test_user_not_found_propagates_unchanged() -> None:
    # The orchestrator's typed handler maps this to USER_NOT_FOUND; a registry that
    # rewrapped it as ToolExecutionError would send the wrong reason.
    with pytest.raises(UserNotFound):
        registry.run("get_user", {"user_id": "u_005"})


def test_a_loaders_tool_error_propagates_with_its_locator() -> None:
    with pytest.raises(ToolExecutionError) as caught:
        registry.run("get_invoices", {"user_id": "u_006"})
    message = str(caught.value)
    assert "inv_601" in message
    assert "forty-two euros" not in message


# --------------------------------------------------------------------------------------
# ADR 0010 -- the registry never sees a recorder or a clock.
# --------------------------------------------------------------------------------------


def test_run_takes_only_a_name_and_args() -> None:
    # Pins the decision structurally: no TraceRecorder parameter, no Clock parameter.
    assert list(inspect.signature(registry.run).parameters) == ["name", "args"]
