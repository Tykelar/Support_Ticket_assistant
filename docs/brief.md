---
title: take-home-challenge-ai-squad
type: note
permalink: claude-docs/hiring/take-home-challenge-ai-squad
---

# Take-Home Challenge — Support Ticket Auto-Reply Service

Thanks for taking the time to do this challenge. It mirrors the kind of system our AI squad builds and runs in production, scaled down to something you can build in a few evenings.

**Time-box: aim for 6–8 hours total.** You have one week from receiving this. We calibrate our expectations to the time-box — a focused, well-reasoned partial solution beats a gold-plated one. If you run out of time, ship what you have and write in the README what you'd do next.

## The problem

Build a small service that automatically replies to customer support tickets for an EV-charging app. Tickets arrive via an API; an AI pipeline decides what data it needs, drafts a reply, and either sends it or hands the ticket off to a human agent.

## Functional requirements

### 1. API
- `POST /tickets` — accepts `{user_id, subject, body}`, returns a ticket id. Processing may be synchronous or asynchronous — your choice, justify it in the README.
- `GET /tickets/{id}` — returns the ticket's status (`processing | replied | handed_off`), the reply text (if any), and the **trace** (see requirement 5).

### 2. Tools
The pipeline gathers data through exactly three tools, backed by local fixture data (JSON files or SQLite — you create the fixtures, ~3–5 users with varied data):
- `get_user(user_id)` → profile (name, language, plan)
- `get_charging_sessions(user_id)` → recent sessions (station, kWh, cost, status)
- `get_invoices(user_id)` → recent invoices (amount, status: paid/pending/failed)

The reply may only contain facts that came from tool results. **Inventing data the tools didn't return is the one unforgivable bug in this domain.**

### 3. The LLM
You do **not** need a real LLM or a paid API key. Define an LLM client interface and implement a **fake**: a deterministic, rule-based implementation that (a) classifies the ticket intent (e.g. billing question, charging-session problem, anything else), (b) decides which tools to call, and (c) produces a reply from a template filled with tool data. Keep it simple — keyword matching is fine.

We are deliberately evaluating the **system around the model** — the loop, the guardrails, the failure handling — not prompt engineering. Wiring a real provider (or a local model via Ollama) behind the same interface is an optional bonus, never at the expense of the core requirements.

### 4. Guardrails
- The tool-calling loop must have a hard iteration cap.
- The pipeline must hand off to a human (status `handed_off`, no reply sent) when: the user or requested data doesn't exist, the intent is outside the supported categories, or any step fails. Failing closed beats replying wrong.
- A handed-off ticket must record *why* it was handed off.

### 5. Traceability
For every ticket, persist a trace of what the pipeline did: each step, each tool call with its arguments and (summarized) result, the final decision and its reason. A support agent must be able to answer "why did the AI say this?" from `GET /tickets/{id}` alone.

### 6. Tests
Automated tests for at least: one happy path end-to-end, the handoff-on-missing-data path, and the iteration cap. Use whatever test framework you prefer.

### 7. Packaging
- Python. Any web framework (FastAPI is a fine choice).
- Runnable with at most two commands from a clean clone (e.g. `docker compose up` or `pip install + one command`). Document them.
- Git repository with a real commit history — we read the history, not just the final tree.

### README
Include: how to run and test; your main design decisions and why (sync vs async, how you structured the pipeline, what you'd change with more time); and one short section — "what I would measure in production to know this is working."

## Rules on AI assistants
You may use AI coding tools (Copilot, Claude, ChatGPT, etc.) — we do, daily. Two conditions: state in the README what you used and how, and be prepared to explain and modify **any** line of the code in the review session. Code you can't defend counts against you far more than not using AI would.

## What happens next
We'll review your submission, then hold a ~45-minute session where you walk us through the design and make one small live change to your own code (e.g. add a tool or a new handoff rule).

## Submission
Push to a private GitHub repository and invite `rgtzths`, or send a zip including the `.git` directory.

Questions about the requirements are welcome at any point — asking good clarifying questions is signal, not weakness.
