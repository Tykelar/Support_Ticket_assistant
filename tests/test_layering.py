"""The dependency direction of ARCHITECTURE.md section 3, enforced mechanically.

The rule is that `llm`, `tools` and `guardrails` know nothing about each other, that
`tracing` sits below all three, and that the root modules stay at the bottom. Nothing in
a review catches a violation reliably -- an import added in one line looks harmless and
only shows up later as a cycle someone works around
([ADR 0011](../docs/adr/0011-shared-vocabulary-below-the-components.md)).

`if TYPE_CHECKING:` imports are deliberately excluded. `llm/templates.py` names `FactSet`
for type-checking and duck-types at runtime; that is a documented arrangement (LLM.md),
not an edge in the dependency graph.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "support_assistant"

MUTUALLY_IGNORANT = ("llm", "tools", "guardrails")
"""ARCHITECTURE.md section 3: "they meet only in the orchestrator"."""

SANCTIONED = {("llm/fake.py", "tools")}
"""The one documented exception (LLM.md): `FakeLLM` checks a tool name against
`registry.registered()` before building the call, so the registry stays the single
source of truth for tool names. Named here rather than waved through, so a *second*
sibling import still fails this test."""


class _RuntimeImports(ast.NodeVisitor):
    """Every `support_assistant.*` module a file imports when it is actually executed."""

    def __init__(self) -> None:
        self.modules: set[str] = set()

    def visit_If(self, node: ast.If) -> None:
        if "TYPE_CHECKING" in ast.dump(node.test):
            return  # a type-only import is not a runtime dependency
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        self.modules.update(
            alias.name for alias in node.names if alias.name.startswith("support_assistant")
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.module.startswith("support_assistant"):
            self.modules.add(node.module)


def _imports(path: Path) -> set[str]:
    visitor = _RuntimeImports()
    visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
    return visitor.modules


def _component(module: str) -> str:
    """`support_assistant.guardrails.factset` -> `guardrails`; `...domain` -> `domain`."""
    return module.removeprefix("support_assistant.").split(".")[0]


def _modules_under(package: str) -> list[Path]:
    return sorted((SRC / package).rglob("*.py"))


# --------------------------------------------------------------------------------------
# The components
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("package", MUTUALLY_IGNORANT)
def test_the_mutually_ignorant_components_do_not_import_each_other(package: str) -> None:
    forbidden = set(MUTUALLY_IGNORANT) - {package}
    offenders = {
        path.relative_to(SRC).as_posix(): sorted(bad)
        for path in _modules_under(package)
        if (bad := {
            m
            for m in _imports(path)
            if _component(m) in forbidden
            and (path.relative_to(SRC).as_posix(), _component(m)) not in SANCTIONED
        })
    }
    assert offenders == {}, (
        f"{package}/ imports a sibling component: {offenders}. They meet in the orchestrator."
    )


def test_tracing_models_stays_below_domain() -> None:
    """`domain` imports `tracing.models` for `Ticket.trace`, so whatever that one module
    reaches for lands underneath `domain` too. `Violation` is defined there rather than in
    `guardrails/` for exactly this reason -- as a guardrail type it dragged the whole
    package below `domain` and split `GroundingChecker` out of `grounding.py` (ADR 0011).

    The rest of `tracing/` is unconstrained: `summarise.py` reads domain records and sits
    happily above them. It is only the module `domain` names that has to stay low.
    """
    assert {_component(m) for m in _imports(SRC / "tracing" / "models.py")} <= {"enums", "clock"}


def test_nothing_in_tracing_reaches_into_a_component() -> None:
    offenders = {
        path.relative_to(SRC).as_posix(): sorted(bad)
        for path in _modules_under("tracing")
        if (bad := {m for m in _imports(path) if _component(m) in MUTUALLY_IGNORANT})
    }
    assert offenders == {}, f"tracing/ must not depend on a component, found {offenders}"


# --------------------------------------------------------------------------------------
# The orchestrator, and what sits under it
# --------------------------------------------------------------------------------------


BELOW_THE_PIPELINE = (*MUTUALLY_IGNORANT, "tracing", "storage")
"""ARCHITECTURE.md section 3: `api --> pipeline --> llm / tools / guardrails / storage`,
and `pipeline --> tracing`. Dependencies point downward from the orchestrator."""


@pytest.mark.parametrize("package", BELOW_THE_PIPELINE)
def test_nothing_below_the_pipeline_imports_it(package: str) -> None:
    """The arrow into `pipeline` comes only from `api`.

    A component reaching back up into the orchestrator is how "the orchestrator is the
    only thing that decides an outcome" (ADR 0005) stops being true: the first such import
    is always a convenience, and the second is a component deciding a handoff for itself.
    """
    offenders = {
        path.relative_to(SRC).as_posix(): sorted(bad)
        for path in _modules_under(package)
        if (bad := {m for m in _imports(path) if _component(m) == "pipeline"})
    }
    assert offenders == {}, f"{package}/ imports pipeline: {offenders}. The arrow points down."


def test_storage_depends_on_no_component() -> None:
    """`storage/` is reached *through* the orchestrator, and knows only the domain, the
    clock, and the trace models it persists. A repository that imported `llm/` or
    `guardrails/` would be a persistence layer with opinions about replies."""
    offenders = {
        path.relative_to(SRC).as_posix(): sorted(bad)
        for path in _modules_under("storage")
        if (bad := {m for m in _imports(path) if _component(m) in (*MUTUALLY_IGNORANT, "pipeline")})
    }
    assert offenders == {}, f"storage/ must not depend on a component, found {offenders}"


ABOVE_NOTHING = (*BELOW_THE_PIPELINE, "pipeline")
"""Every package that is not `api`. The arrow into `api` comes from outside the process."""


@pytest.mark.parametrize("package", ABOVE_NOTHING)
def test_nothing_imports_the_api(package: str) -> None:
    """ARCHITECTURE.md section 3: "Nothing imports `api`". It is the entry point, so an
    import of it is something below the pipeline reaching for a request, a response model
    or the app itself -- which is how HTTP concerns start leaking into the domain."""
    offenders = {
        path.relative_to(SRC).as_posix(): sorted(bad)
        for path in _modules_under(package)
        if (bad := {m for m in _imports(path) if _component(m) == "api"})
    }
    assert offenders == {}, f"{package}/ imports api: {offenders}. Nothing imports the edge."


def test_the_api_reaches_no_tool_and_no_guardrail() -> None:
    """API.md: the HTTP layer validates input, schedules work and serves ticket state. It
    may name `pipeline` and `storage` (it wires them) and `llm` (it picks the default
    client), but a tool call or a grounding check reached from a request handler is
    pipeline logic that has escaped the orchestrator -- and escaped the trace with it.
    """
    offenders = {
        path.relative_to(SRC).as_posix(): sorted(bad)
        for path in _modules_under("api")
        if (bad := {m for m in _imports(path) if _component(m) in ("tools", "guardrails")})
    }
    assert offenders == {}, f"api/ reaches into a component the orchestrator owns: {offenders}"


def test_the_orchestrator_is_where_the_components_meet() -> None:
    """The other half of the mutual-ignorance rule. `llm`, `tools` and `guardrails` never
    import each other, so *something* has to hold all three -- and if nothing does, the
    rule is being kept by the components not actually being wired together."""
    imported = {
        _component(m) for m in _imports(SRC / "pipeline" / "orchestrator.py")
    }
    assert set(MUTUALLY_IGNORANT) <= imported
    assert "storage" in imported


# --------------------------------------------------------------------------------------
# The root modules
# --------------------------------------------------------------------------------------


def test_domain_imports_only_the_layers_below_it() -> None:
    """ARCHITECTURE.md draws `everything --> domain / enums / clock`. `domain` naming a
    component package inverts that arrow, and it is what forced `GroundingChecker` out of
    `grounding.py` before ADR 0011."""
    assert {_component(m) for m in _imports(SRC / "domain.py")} <= {"enums", "tracing", "clock"}


@pytest.mark.parametrize("module", ["enums.py", "clock.py"])
def test_the_bottom_modules_import_nothing_from_the_package(module: str) -> None:
    assert _imports(SRC / module) == set()


def test_every_sanctioned_exception_is_still_real() -> None:
    # An exemption that no longer describes the code is worse than no exemption -- it is a
    # hole held open for nothing. This fails when the sanctioned import is removed.
    for relative, component in SANCTIONED:
        imported = {_component(m) for m in _imports(SRC / relative)}
        assert component in imported, f"{relative} no longer imports {component}; drop the waiver"


def test_the_guard_can_actually_fail(tmp_path: Path) -> None:
    # A guard that cannot fail is decoration.
    offender = tmp_path / "offender.py"
    offender.write_text(
        "from typing import TYPE_CHECKING\n"
        "from support_assistant.guardrails.factset import FactSet\n"
        "if TYPE_CHECKING:\n"
        "    from support_assistant.llm.templates import Template\n",
        encoding="utf-8",
    )
    found = {_component(m) for m in _imports(offender)}
    assert found == {"guardrails"}  # the runtime import is seen, the TYPE_CHECKING one is not
