# Tests

pytest. Every test is deterministic and offline — no network, no clock dependence, no
randomness, no sleeps.

That is a design property, not luck. Three things make it hold:

- `FakeLLM` has no clock and no RNG
  ([ADR 0006](../docs/adr/0006-fake-first-llm-behind-a-client-protocol.md));
- time is injected, never ambient — tests run a `FrozenClock` that starts at a fixed
  instant and advances **10ms per call**
  ([ADR 0008](../docs/adr/0008-injected-clock-with-advancing-test-double.md));
- Starlette's `TestClient` drains background tasks before returning the response
  ([ADR 0001](../docs/adr/0001-asynchronous-in-process-processing.md)), so an end-to-end
  test can `POST` and immediately `GET` a terminal status with no polling and no race.

The advancing tick is what makes timing assertable rather than merely deterministic: a
known tick times a known number of steps is arithmetic, so `pipeline_duration_seconds` is
covered like anything else, and a step recorded out of order shows up as a timestamp that
moves backwards — which identical timestamps would hide.

---

## Running them

```bash
pip install -e ".[dev]"
pytest
```

Or inside the container: `docker compose run --rm api pytest`.
Full commands in [PACKAGING.md](../deploy/PACKAGING.md).

---

## The three the brief requires

Named explicitly so they are easy to find and to run in isolation.

The first two go through `POST` and `GET`, and now that `api/` exists they run. The pipeline half of
each is already covered by `test_pipeline.py` -- the happy path with a grounded reply, and
both handoff cases keeping their separate reasons -- so what the e2e files add is the HTTP
surface and the background-task scheduling, not the behaviour.

### 1. Happy path, end to end &nbsp;·&nbsp; *built*

`test_e2e_happy_path.py::test_billing_question_gets_grounded_reply`

`POST` a billing ticket for `u_002` (who has one failed invoice among paid ones) → `202`.
`GET` → `status == "replied"`.

Asserts, in order of what actually matters:

- the reply names the real invoice id and the real amount from the fixture;
- **every literal in the reply appears in the fixture data** — the grounding property
  asserted directly on the output, not merely trusted because the checker ran. The
  identifiers, numbers and status words are extracted with the test's *own* regexes and
  compared against the raw `fixtures/*.json`, never against the `FactSet` the pipeline
  built: a test that borrowed the checker's idea of a literal could not fail when that
  idea is the thing that is wrong;
- the trace contains `intent_classified`, both `tool_call`/`tool_result` pairs,
  `grounding_check` passed, and `final_decision`;
- `handoff_reason` is `None`.

```bash
pytest tests/test_e2e_happy_path.py -v
```

### 2. Handoff on missing data &nbsp;·&nbsp; *built*

`test_e2e_handoff.py::test_unknown_user_hands_off`
`test_e2e_handoff.py::test_known_user_with_no_data_hands_off`

Two cases, because the brief distinguishes "the user doesn't exist" from "the requested
data doesn't exist" and they must not collapse into one reason.

| Case | User | Expected reason |
|---|---|---|
| user absent from fixtures | `u_005` | `USER_NOT_FOUND` |
| user exists, no invoices or sessions | `u_004` | `DATA_NOT_FOUND` |

Both assert `reply is None` — not empty string, not a holding message — and that
`final_decision` carries the reason **and** its supporting detail.

```bash
pytest tests/test_e2e_handoff.py -v
```

Both files run over a **real `SqliteTicketRepository`** in `tmp_path` rather than the
in-memory double. The pipeline behaviour is already covered elsewhere; what these add is
the HTTP surface, the background-task scheduling and the serialisation round trip — and
that last one is where a trace-ordering defect hid until a manual end-to-end run found it.
Their shared fixtures are in `conftest.py`.

### 3. Iteration cap &nbsp;·&nbsp; *built*

`test_iteration_cap.py::test_runaway_loop_hits_cap`

A stub `LLMClient` that returns `ToolCall` forever. The ticket must reach `handed_off`
with `ITERATION_CAP_EXCEEDED`.

Asserts the **exact iteration count**, not just the outcome — `llm_decision` steps in the
trace must number exactly `MAX_ITERATIONS`. An off-by-one that runs six times still
produces the right status, and would pass a weaker test.

Also asserts no reply was produced, and that the run terminates without the test needing a
timeout.

```bash
pytest tests/test_iteration_cap.py -v
```

---

## The rest of the suite

### The one that guards the unforgivable bug

`test_grounding.py::test_ungrounded_literal_is_caught`

Renders through a **deliberately doctored template** that injects an amount absent from
the tool results. The ticket must reach `handed_off` / `UNGROUNDED_REPLY`, with the
offending literal recorded in the `grounding_check` trace step.

This is the test that proves the brief's "one unforgivable bug" cannot ship. It is worth
more than the happy path, because the happy path passing tells you the system works when
nothing is wrong.

The doctored template is a real `llm.templates.Template` — production dataclass, real
field selection, real safe-literal set — with one amount changed, rendered through the
same `Template.render` a genuine one uses. A control alongside it renders the *undoctored*
template against the same facts and asserts it is clean, so a failure means the injected
amount and not something incidental in the prose.

> **Built in two halves, and both are built.** The checker half asserts the `Violation`
> directly. The terminal-state half —
> `test_an_ungrounded_literal_withholds_the_reply_and_hands_the_ticket_off` — runs the same
> doctored template through the real orchestrator and asserts `reply is None`,
> `handed_off` / `UNGROUNDED_REPLY`, and the literal in both the `grounding_check` step and
> the `final_decision` detail. The first half alone would pass a system that caught the
> literal and sent the reply anyway. A control replies honestly through the same pipeline,
> so handing off everything is not a way to pass.

Alongside it: normalisation cases (`42.10` / `42,10` / `42.1` are the same fact),
`TEMPLATE_SAFE_LITERALS` accepted, unknown identifiers rejected, and a `Violation`
recording the literal with the spelling the reply used rather than a folded one.

And the pair that keeps the entity mask honest: a station name containing a digit renders
clean, while a station name the `FactSet` does **not** hold is still scanned and still
fails. A mask that swallowed both would be a hole, not a fix.

### By component

| File | Covers |
|---|---|
| `test_domain.py` | the enum members as a cross-component contract; ticket id shape and uniqueness; the status/reply/reason invariant in every direction, including `reply=""` on a handoff |
| `test_fixtures.py` | the fixture data as data — every path has a user, `u_005` absent from every file, rows newest-first, and `u_006` malformed while `u_002` parses |
| `test_tools.py` | the three tools; `UserNotFound` / `NoDataAvailable` / `ToolExecutionError`; empty-list-is-not-success (ADR 0009); **`u_006`'s malformed row fails without breaking `u_002`** |
| `test_registry.py` | dispatch, unregistered name rejected, argument schema validation |
| `test_fake_llm.py` | keyword rules per intent, **tie resolves to `unknown`**, step state machine ordering |
| `test_factset.py` | projection from observations, `allowed_literals()`, `allowed_entities()` |
| `test_grounding.py` | the checker, as above; sourced entity text not re-scanned; `TEMPLATE_SAFE_LITERALS` is numbers-only; every status enum member extractable |
| `test_layering.py` | the dependency direction of ARCHITECTURE.md §3, parsed from the imports (ADR 0011); nothing below `pipeline/` imports it, nothing at all imports `api/`, `api/` reaches no tool and no guardrail, and the orchestrator is where the three mutually-ignorant components meet |
| `test_repository_contract.py` | **parametrised over both implementations**; the trace round-trip, and the SQLite-only `CHECK` backstop |
| `test_tracing.py` | step ordering, `seq` monotonicity, **`ts` increasing with `seq`**, summarisation rules |
| `test_api.py` | `202` shape, `404`, field mutual exclusion per status, `422` on bad input; that a `POST` really schedules the run and `TestClient` really drains it; `class` as the served key of a `Violation`; and the lifespan's repository ownership |
| `test_docs.py` | API.md's worked examples, driven through a real request and checked against what the code actually returns — the same failure class as `test_layering.py`, applied to documentation instead of imports |
| `test_clock.py` | `FrozenClock` advances exactly one tick per call; duration derived from trace timestamps; **a grep guard that no module outside `clock.py` reads the wall clock** |
| `test_pipeline.py` | orchestration with stubbed collaborators; every handoff reason reachable, and what every handoff owes regardless of reason |
| `test_iteration_cap.py` | the cap, self-contained — one of the three the brief names |

### Two that are easy to omit and worth keeping

**`test_repository_contract.py` runs against both `SqliteTicketRepository` and
`InMemoryTicketRepository`.** A test double that has drifted from the real implementation
is worse than no double — it makes the whole suite confidently wrong. This is the test
that keeps them honest ([STORAGE.md](../src/support_assistant/storage/STORAGE.md)).

**`test_layering.py` parses every module's imports rather than trusting the diagram.**
A component boundary is the kind of thing that erodes one convenient import at a time, and
each one looks harmless in review — ADR 0011 exists because that already happened once. It
skips `if TYPE_CHECKING:` blocks, and it names the single sanctioned `llm → tools` import
so that a second one fails.

**`test_pipeline.py` asserts every `HandoffReason` member is reachable.** A reason that no
code path can produce is dead code pretending to be a guardrail; a parametrised test over
the enum catches one being added without being wired.

---

## Strategy

Mostly unit tests with a thin end-to-end layer. The pipeline is tested against stubbed
collaborators, so a failing test names the broken component rather than reporting that
"the pipeline is broken".

Fixtures are shared with the running service — tests read the same
`src/support_assistant/fixtures/*.json`. Divergence between test data and demo data is a
class of bug not worth inviting, and the fixtures were designed so every path has a user
that exercises it ([TOOLS.md](../src/support_assistant/tools/TOOLS.md)).

Stubs, not mocks. Each stub is a small class implementing the real protocol, so a protocol
change breaks the stubs at type-check time instead of leaving tests passing against an
interface that no longer exists.

**Not measured by coverage percentage.** The suite is organised around the failure modes
the brief names — invented data, unbounded loops, silent failures — because a high
coverage number on a system that can invent data would be a false comfort.

---

## What is not tested

- `OllamaLLM` beyond a request-shape unit test — testing it properly needs a model
  server, which would make the suite non-deterministic and non-offline. It is off by
  default and additive.
  [Roadmap](../docs/ROADMAP.md#hardening-the-real-llm-client).
- Concurrency and load. Single-process, in-memory background tasks; a real question this
  suite does not answer. [Roadmap](../docs/ROADMAP.md#bounded-concurrency).
- The Docker image itself, beyond it building and the service responding.
