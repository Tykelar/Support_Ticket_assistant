"""The dispatch chokepoint. The loop never imports a tool directly -- it calls
`registry.run(name, args)` and gets a `ToolResult` back.

Two jobs, and only two:

1. **Containment.** A model can only reach a registered name; an unrecognised one is a
   `ToolExecutionError`, not an `AttributeError`.
2. **Argument validation.** Each entry declares a Pydantic schema, so bad arguments fail
   before the tool body runs.

Tracing is deliberately not a third job: the orchestrator records `tool_call` and
`tool_result` around its own call, so `tools/` imports nothing from `tracing/` (ADR 0010).
Typed tool failures propagate unchanged, so the orchestrator's handlers still map
`UserNotFound` -> `USER_NOT_FOUND`.

**Adding a tool touches more than this file.** Beyond the entry here and its loader, it
needs a summariser in `tracing/summarise.py`, a projection branch in
`guardrails/factset.py`, a member of `domain.ToolResult.records`, a rule in `FakeLLM`, a
line in `OllamaLLM`'s catalogue, and a fixture.
`test_registry.py::test_every_registered_tool_is_summarised_and_projected` fails on the
two that would otherwise break only at runtime.
"""

from collections.abc import Callable
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from support_assistant.domain import FixtureRecord, ToolResult
from support_assistant.tools import loaders
from support_assistant.tools.errors import ToolExecutionError, failed_fields


class _UserIdArgs(BaseModel):
    """Every current tool takes exactly one argument. `extra="forbid"`, so an unexpected
    key fails here rather than being dropped."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)


class _Entry(NamedTuple):
    fn: Callable[[str], FixtureRecord | list[FixtureRecord]]
    args: type[BaseModel]


_REGISTRY: dict[str, _Entry] = {
    "get_user": _Entry(loaders.get_user, _UserIdArgs),
    "get_charging_sessions": _Entry(loaders.get_charging_sessions, _UserIdArgs),
    "get_invoices": _Entry(loaders.get_invoices, _UserIdArgs),
}


def run(name: str, args: dict[str, Any]) -> ToolResult:
    """Dispatch `name` with `args`, wrapping the tool's return in a `ToolResult`.

    Raises `ToolExecutionError` for an unregistered name or arguments that fail the
    schema. Any exception the tool itself raises propagates untouched.
    """
    entry = _REGISTRY.get(name)
    if entry is None:
        raise ToolExecutionError(f"no tool named {name!r}")
    try:
        parsed = entry.args.model_validate(args)
    except ValidationError as exc:
        raise ToolExecutionError(
            f"{name} called with invalid arguments: {failed_fields(exc)}"
        ) from exc
    returned = entry.fn(**parsed.model_dump())
    records = returned if isinstance(returned, list) else [returned]
    return ToolResult(tool=name, records=records)


def registered() -> tuple[str, ...]:
    """The tool names the loop may dispatch, for `FakeLLM`'s keyword rules and for tests."""
    return tuple(_REGISTRY)
