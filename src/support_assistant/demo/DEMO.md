# Demo

A page that shows the pipeline working, and a set of tickets that exercise every path it
can take.

Not part of the service. `/docs` already lets you fire a request and read the JSON that
comes back; what it cannot show is the pipeline as a *process* — classify, decide, call a
tool, read the result, decide again, ground the draft, commit an outcome. That sequence is
what the trace records and what this project is actually about, so it deserves to be
legible without reading raw JSON.

---

## Open it

```bash
uvicorn support_assistant.api.app:app --reload   # or docker compose -f deploy/docker-compose.yml up
```

Then <http://localhost:8000/ui>. Press **Seed all ten scenarios**.

Or from a terminal, against a service that is already running:

```bash
python -m support_assistant.demo.seed
```

The seeder goes in through the front door — `POST /tickets`, then `GET` until the status is
terminal — rather than writing rows into the database. A seeder that inserted tickets
directly would produce states the pipeline never actually reached, which is the one thing a
demo of this system must not do. Every scenario declares its expected outcome, so the
command doubles as a smoke test over HTTP and exits non-zero if a ticket lands anywhere
else.

---

## The ten scenarios

Curated, not random. Random tickets would produce a lopsided spread and prove nothing;
these are chosen against the fixture map in [TOOLS.md](../tools/TOOLS.md) so that **every
terminal state reachable through the API has a ticket demonstrating it**. The submission
order *is* shuffled, so the resulting list is not sorted by outcome.

| Scenario | User | Outcome | Why it is here |
|---|---|---|---|
| Nothing outstanding | `u_001` | `replied` | the happy path with nothing wrong |
| A payment that failed | `u_002` | `replied` | a real problem to explain; the reply names `inv_204` and `42.10` |
| An invoice still pending | `u_003` | `replied` | the only template with a static number in its prose, declared in `TEMPLATE_SAFE_LITERALS` |
| A session that finished normally | `u_001` | `replied` | same user as the first, different intent — the classifier picks the tool, not the user |
| A session that cut out | `u_003` | `replied` | the reply states a status word, which grounding layer 2 checks |
| A user with no data at all | `u_004` | `handed_off` `DATA_NOT_FOUND` | zero rows is a handoff, not "you have no invoices" ([ADR 0009](../../../docs/adr/0009-absent-data-is-a-handoff.md)) |
| A user who does not exist | `u_005` | `handed_off` `USER_NOT_FOUND` | still a `202`: whether a user exists is a fact only a tool can establish |
| A malformed record upstream | `u_006` | `handed_off` `TOOL_ERROR` | the failure reaches the trace as a locator, never the bad value |
| A question the tools cannot answer | `u_002` | `handed_off` `UNSUPPORTED_INTENT` | no fourth tool, so the honest answer is a human |
| An ambiguous ticket | `u_002` | `handed_off` `UNSUPPORTED_INTENT` | one billing word, one charging word: a tie fails closed |

`tests/test_demo.py` drives all ten through the real pipeline and asserts each reaches the
outcome it claims, so a scenario cannot quietly start demonstrating something else.

---

## What it cannot show, and why that is stated rather than faked

**Two handoff reasons never appear.** `ITERATION_CAP_EXCEEDED` needs a model that never
stops, and `UNGROUNDED_REPLY` needs a renderer that emits a literal no tool returned.
`FakeLLM` terminates in three iterations and never returns `Handoff`, and grounding layer 1
renders only from the `FactSet` — so neither is reachable by posting a ticket
([ADR 0004](../../../docs/adr/0004-two-layer-grounding-enforcement.md),
[ADR 0006](../../../docs/adr/0006-fake-first-llm-behind-a-client-protocol.md)). Both are
reached in the suite by injecting a misbehaving client, in `test_iteration_cap.py` and
`test_grounding.py`. The page's coverage panel greys them out and says so.

**`processing` flashes past.** It is a real state, and the page will render it — but under
the deterministic `FakeLLM` a run takes milliseconds. Adding a sleep to make the spinner
look impressive would be a lie about the system's behaviour. Set `LLM_PROVIDER=ollama`
([PACKAGING.md](../../../deploy/PACKAGING.md)) and the wait becomes real.

**The two ambiguity scenarios look identical in the trace.** An unknown intent has no
evidence for it, so `matched_keywords` is empty for both
([ADR 0012](../../../docs/adr/0012-classification-carries-its-own-evidence.md)). The
scenarios differ in cause, not in what the system can honestly record.

---

## How the page finds tickets

It remembers the ids it was given, in `localStorage`. There is no listing endpoint, and
adding one for a demo would have been the wrong trade: a `GET /tickets` is unauthenticated
bulk access to customer data, which is exactly why
[the roadmap defers it](../../../docs/ROADMAP.md#cross-ticket-queries).

So the page ends up demonstrating the service's actual access model rather than
contradicting it — **the ticket id is the only key** ([API.md](../api/API.md#security-what-is-not-here-stated-once)).
Clearing browser storage orphans earlier tickets; they still exist, and the "open by id"
box takes an id printed by the seeder.

---

## Structure

```
demo/
  __init__.py      STATIC_DIR, SCENARIOS_FILE, load_scenarios()
  seed.py          the CLI seeder -- stdlib urllib, no new dependency
  static/
    index.html     the page
    app.js         vanilla JS: no build step, no framework, no CDN
    style.css
    scenarios.json the ten scenarios -- read by the browser, the seeder and the tests
  DEMO.md          this file
```

**The demo imports nothing from the service.** `api/app.py` mounts `static/` at `/ui` and
that mount is the only wire; everything else goes over HTTP like any other client.
`test_layering.py::test_the_demo_package_imports_nothing_but_itself` enforces it, because a
demo aid that reached for the orchestrator would become a second way to produce a ticket —
one whose states the pipeline never actually reached.

`scenarios.json` lives under `static/` so the browser fetches the same file the seeder and
the tests read. One copy, so a scenario cannot be fixed in one place and stay broken in the
other — the same reasoning that keeps the suite reading the service's own
[tool fixtures](../fixtures/).

**Everything rendered from a response is written with `textContent`.** A handoff detail can
contain text that came from the ticket, and `innerHTML` would turn the trace into an
injection sink.
