# Packaging

> Runnable with at most two commands from a clean clone.

Two paths, both meeting that. Docker is the primary one because it needs nothing installed
but Docker.

---

## Run it — Docker (recommended)

```bash
git clone <repo> && cd support-ticket-assistant
docker compose up
```

Service on `http://localhost:8000`. Interactive API docs at `/docs`.

## Run it — local Python

```bash
pip install -e ".[dev]"
uvicorn support_assistant.api.app:app --reload
```

Python 3.12+.

## Test it

```bash
pytest                                   # local
docker compose run --rm api pytest       # in the container
```

---

## Try it

```bash
# A billing question for a user with a failed invoice -> grounded reply
curl -s -X POST localhost:8000/tickets \
  -H 'content-type: application/json' \
  -d '{"user_id":"u_002","subject":"My payment failed",
       "body":"I got an email saying my invoice could not be charged. What happened?"}'
# -> {"id":"t_...","status":"processing"}

curl -s localhost:8000/tickets/t_... | jq
# -> status "replied", the reply, and the full trace

# A user who does not exist -> handoff, no reply
curl -s -X POST localhost:8000/tickets \
  -H 'content-type: application/json' \
  -d '{"user_id":"u_005","subject":"Billing question","body":"Why was I charged?"}'
# -> GET shows status "handed_off", reason "USER_NOT_FOUND", reply null
```

Which user demonstrates which path is in [TOOLS.md](../src/support_assistant/tools/TOOLS.md).

---

## Configuration

Every variable has a working default. **A clean clone needs no configuration at all** —
that is the point, and it is why the optional LLM is opt-in rather than opt-out.

| Variable | Default | Meaning |
|---|---|---|
| `MAX_ITERATIONS` | `5` | hard cap on tool-loop iterations ([GUARDRAILS.md](../src/support_assistant/guardrails/GUARDRAILS.md)) |
| `DATABASE_PATH` | `data/tickets.db` | SQLite file; `:memory:` for a throwaway run |
| `LLM_PROVIDER` | `fake` | `fake` or `ollama` |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | only read when `LLM_PROVIDER=ollama` |
| `LOG_LEVEL` | `info` | structured JSON log level |

---

## The image

- `python:3.12-slim` base.
- Multi-stage: dependencies resolved in a build stage, only the venv and source copied
  into the runtime stage. Smaller image, and build tooling stays out of what ships.
- Runs as a non-root user.
- `HEALTHCHECK` on `GET /health`.
- Source is installed with `pip install -e .`, so `docker compose run --rm api pytest`
  works in the same image that serves traffic — the tests run against what ships.

### The volume, and why it is not incidental

```yaml
volumes:
  - ticket-data:/app/data
```

The SQLite file lives on a named volume so traces survive `docker compose restart`.
Without it, "the trace is persisted" ([ADR 0003](../docs/adr/0003-sqlite-behind-a-repository-protocol.md))
would be a claim the demo itself contradicts the first time someone restarts the stack.

### One service

No database container, no broker, no cache. SQLite is a file and the pipeline runs
in-process (ADR 0001 / ADR 0003). A `docker compose` that starts one thing is a truthful
description of the architecture, not a shortcut.

---

## Structure

```
deploy/
  Dockerfile
  docker-compose.yml
  .dockerignore
  PACKAGING.md      this file
```

`.dockerignore` excludes `.git`, `data/`, caches, and the virtualenv — build context stays
small and no local database is baked into an image.

---

## Commit history

The brief says the history is read, not just the final tree. This repository's history is
built to be read:

- one commit per meaningful step, in the order the work happened;
- documentation and ADRs land before the code they describe, because the design decisions
  were made first;
- tests land before their implementation, per the project's own working rules;
- messages say **why**, not just what.

The history was started fresh for this challenge, so the first commit is this project's
scaffolding rather than unrelated template noise
([ADR 0007](../docs/adr/0007-component-packages-with-colocated-docs.md) covers the layout
that commit establishes).
