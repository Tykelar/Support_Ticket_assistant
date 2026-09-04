"use strict";

/* The demo page.
 *
 * Talks to the two endpoints the brief defines and nothing else: POST /tickets to submit,
 * GET /tickets/{id} to read one back. There is no listing endpoint (ROADMAP: cross-ticket
 * queries), so the ids this browser has been given are kept in localStorage -- which is
 * also an honest demonstration of the service's access model: the id is the only key.
 *
 * Everything rendered from a response is written with textContent. A handoff detail can
 * contain text that came from the ticket, and innerHTML would turn the trace into an
 * injection sink.
 */

const STORAGE_KEY = "support-assistant.demo.tickets";
const POLL_MS = 250;
const POLL_LIMIT = 40;

const REASONS = [
  ["USER_NOT_FOUND", null],
  ["DATA_NOT_FOUND", null],
  ["UNSUPPORTED_INTENT", null],
  ["TOOL_ERROR", null],
  ["ITERATION_CAP_EXCEEDED", "needs a model that never stops: injected in tests, unreachable from here"],
  ["UNGROUNDED_REPLY", "needs a renderer that invents a literal: injected in tests, unreachable from here"],
];
const TEMPLATE_COUNT = 5;

/** [{id, label}] -- what this browser holds, oldest first. */
let held = restore();
/** id -> the served ticket. */
const tickets = new Map();
let scenarios = [];
let selected = null;

const $ = (id) => document.getElementById(id);

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

// ---------------------------------------------------------------------- storage

function restore() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    return Array.isArray(raw) ? raw.filter((t) => t && typeof t.id === "string") : [];
  } catch {
    return [];
  }
}

function remember(id, label) {
  held.push({ id, label });
  localStorage.setItem(STORAGE_KEY, JSON.stringify(held));
}

function forget(id) {
  held = held.filter((entry) => entry.id !== id);
  tickets.delete(id);
  if (selected === id) selected = null;
  localStorage.setItem(STORAGE_KEY, JSON.stringify(held));
  render();
}

function forgetAll() {
  held = [];
  tickets.clear();
  selected = null;
  localStorage.removeItem(STORAGE_KEY);
  render();
}

// ------------------------------------------------------------------------- api

async function createTicket(payload) {
  const response = await fetch("/tickets", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(`POST /tickets returned ${response.status}: ${await response.text()}`);
  }
  return response.json();
}

async function readTicket(id) {
  const response = await fetch(`/tickets/${encodeURIComponent(id)}`);
  if (response.status === 404) throw new Error("no ticket with that id");
  if (!response.ok) throw new Error(`GET returned ${response.status}`);
  return response.json();
}

/** Read a ticket, and keep reading while it is still processing. */
async function poll(id, attempt = 0) {
  const ticket = await readTicket(id);
  tickets.set(id, ticket);
  render();
  if (ticket.status === "processing" && attempt < POLL_LIMIT) {
    setTimeout(() => poll(id, attempt + 1).catch(showError), POLL_MS);
  }
}

async function send(payload, label) {
  const accepted = await createTicket(payload);
  remember(accepted.id, label);
  selected = accepted.id;
  tickets.set(accepted.id, { id: accepted.id, status: accepted.status, trace: [] });
  render();
  await poll(accepted.id);
}

// -------------------------------------------------------------------- rendering

function render() {
  renderTickets();
  renderDetail();
  renderCoverage();
}

function statusPill(status) {
  return node("span", `pill pill-${status}`, status.replace("_", " "));
}

function renderTickets() {
  const list = $("tickets");
  list.replaceChildren();
  $("tickets-empty").hidden = held.length > 0;

  for (const entry of held) {
    const ticket = tickets.get(entry.id);
    const status = ticket ? ticket.status : "processing";

    const button = node("button", null);
    button.type = "button";
    if (entry.id === selected) button.setAttribute("aria-current", "true");
    button.addEventListener("click", () => {
      selected = entry.id;
      render();
    });

    button.append(node("span", "ticket-label", entry.label));
    const meta = node("span", "ticket-meta");
    meta.append(statusPill(status));
    if (ticket && ticket.handoff_reason) meta.append(node("span", null, ticket.handoff_reason));
    meta.append(node("span", "mono", entry.id.slice(0, 8) + "…"));
    button.append(meta);

    const item = document.createElement("li");
    item.append(button);
    list.append(item);
  }
}

function renderDetail() {
  const panel = $("detail");
  panel.replaceChildren();

  const ticket = selected ? tickets.get(selected) : null;
  if (!ticket) {
    panel.append(node("p", "note", "Select a ticket to see its outcome and the trace behind it."));
    return;
  }

  const head = node("div", "outcome");
  head.append(statusPill(ticket.status));
  head.append(node("span", "id mono", ticket.id));
  panel.append(head);

  if (ticket.status === "processing") {
    panel.append(node("p", "note",
      "Still running. The POST returned before the pipeline finished, which is the only "
      + "reason this state is observable at all — under the deterministic FakeLLM it lasts "
      + "milliseconds."));
  }

  if (ticket.reply) {
    panel.append(node("h3", "section", "The reply the customer receives"));
    panel.append(node("pre", "reply", ticket.reply));
  }

  if (ticket.handoff_reason) {
    const box = node("div", "handoff");
    box.append(node("strong", null, ticket.handoff_reason));
    box.append(node("span", null, detailOf(ticket) || "handed to a human; no reply was sent"));
    panel.append(node("h3", "section", "No reply was sent"));
    panel.append(box);
  }

  if (ticket.trace && ticket.trace.length) {
    panel.append(node("h3", "section", `Trace — ${ticket.trace.length} steps`));
    panel.append(timeline(ticket.trace));
    panel.append(node("p", "timing", `Run took ${duration(ticket)} ms wall clock.`));
  } else if (ticket.status !== "processing") {
    panel.append(node("p", "note", "No trace recorded."));
  }
}

function detailOf(ticket) {
  const last = (ticket.trace || []).at(-1);
  return last && last.type === "final_decision" ? last.detail : null;
}

function duration(ticket) {
  return Date.parse(ticket.updated_at) - Date.parse(ticket.created_at);
}

// --------------------------------------------------------------------- timeline

function timeline(trace) {
  const list = node("ol", "trace");
  const start = Date.parse(trace[0].ts);

  for (const step of trace) {
    const described = describe(step);
    const item = node("li", described.cls ? `step ${described.cls}` : "step");

    const head = node("div", "step-head");
    head.append(node("span", "step-title", described.title));
    head.append(node("span", "step-when", `#${step.seq} · +${Date.parse(step.ts) - start}ms`));
    item.append(head);

    for (const [key, value] of described.rows) {
      const line = node("div", "step-body");
      line.append(node("span", "kv", `${key}: `));
      line.append(node("span", "mono", value));
      item.append(line);
    }

    if (described.note) item.append(node("div", "step-body kv", described.note));

    if (described.chips && described.chips.length) {
      const chips = node("div", "chips");
      for (const chip of described.chips) chips.append(node("span", "chip", chip));
      item.append(chips);
    }

    for (const violation of described.violations || []) {
      item.append(node("div", "step-body violation",
        `${violation.literal} (${violation["class"]}) — ${violation.reason}`));
    }

    list.append(item);
  }
  return list;
}

function describe(step) {
  switch (step.type) {
    case "intent_classified":
      return {
        title: "Intent classified",
        cls: step.intent === "unknown" ? "is-bad" : "is-decision",
        rows: [["intent", step.intent]],
        chips: step.matched_keywords,
        note: step.matched_keywords.length
          ? null
          : "no keyword evidence — an unknown intent has none to give",
      };

    case "llm_decision":
      return {
        title: `Model decided · iteration ${step.iteration}`,
        cls: "is-decision",
        rows: step.tool ? [["decision", step.decision], ["tool", step.tool]]
                        : [["decision", step.decision]],
      };

    case "tool_call":
      return {
        title: "Tool call",
        rows: [["tool", step.tool], ["args", JSON.stringify(step.args)]],
      };

    case "tool_result":
      return {
        title: "Tool result",
        cls: step.ok ? "is-good" : "is-bad",
        rows: [["tool", step.tool], ["ok", String(step.ok)], ...summaryRows(step)],
        chips: step.summary && step.summary.referenced,
      };

    case "grounding_check":
      return {
        title: "Grounding check",
        cls: step.passed ? "is-good" : "is-bad",
        rows: [
          ["passed", String(step.passed)],
          ["literals checked", String(step.literals_checked)],
        ],
        violations: step.violations,
      };

    case "final_decision":
      return {
        title: "Final decision",
        cls: step.outcome === "replied" ? "is-good" : "is-bad",
        rows: [
          ["outcome", step.outcome],
          ...(step.reason ? [["reason", step.reason]] : []),
          ...(step.detail ? [["detail", step.detail]] : []),
        ],
      };

    default:
      return { title: step.type, rows: [] };
  }
}

/** A tool result's summary, flattened. `referenced` is rendered as chips instead. */
function summaryRows(step) {
  if (step.error) return [["error", `${step.error.type}: ${step.error.message}`]];
  const rows = [];
  for (const [key, value] of Object.entries(step.summary || {})) {
    if (key === "referenced") continue;
    rows.push([key, typeof value === "object" ? pairs(value) : String(value)]);
  }
  return rows;
}

const pairs = (object) =>
  Object.entries(object).map(([key, value]) => `${key} ${value}`).join(", ");

// --------------------------------------------------------------------- coverage

function renderCoverage() {
  const panel = $("coverage");
  panel.replaceChildren();

  const seen = new Map();
  const replies = new Set();
  for (const ticket of tickets.values()) {
    if (ticket.handoff_reason) {
      seen.set(ticket.handoff_reason, (seen.get(ticket.handoff_reason) || 0) + 1);
    }
    if (ticket.reply) replies.add(ticket.reply);
  }

  const reasons = node("div", "coverage-group");
  reasons.append(node("h4", null, "Handoff reasons"));
  for (const [reason, unreachable] of REASONS) {
    const count = seen.get(reason) || 0;
    const state = unreachable ? "unreachable" : count ? "seen" : "unseen";
    const row = node("div", `coverage-row ${state}`);
    row.append(node("span", "mark", count ? "✓" : unreachable ? "—" : "·"));
    row.append(node("span", null, reason));
    if (count) row.append(node("span", "why", `×${count}`));
    if (unreachable) row.append(node("span", "why", unreachable));
    reasons.append(row);
  }
  panel.append(reasons);

  const templates = node("div", "coverage-group");
  templates.append(node("h4", null, "Replies"));
  const complete = replies.size === TEMPLATE_COUNT;
  const row = node("div", `coverage-row ${complete ? "seen" : "unseen"}`);
  row.append(node("span", "mark", complete ? "✓" : "·"));
  row.append(node("span", null,
    `${replies.size} of ${TEMPLATE_COUNT} reply templates produced`));
  templates.append(row);
  templates.append(node("p", "note",
    "Counted as distinct reply bodies: the API never serves the template name, so this is "
    + "the only honest evidence a different one ran."));
  panel.append(templates);
}

// ----------------------------------------------------------------------- events

function showError(error) {
  const existing = document.querySelector(".error");
  if (existing) existing.remove();
  const message = node("p", "error", String(error.message || error));
  $("ticket-form").append(message);
}

function scenarioOptions() {
  const select = $("scenario");
  for (const scenario of scenarios) {
    const option = document.createElement("option");
    option.value = scenario.id;
    option.textContent = scenario.label;
    select.append(option);
  }
  select.addEventListener("change", () => {
    const scenario = scenarios.find((s) => s.id === select.value);
    $("scenario-note").textContent = scenario
      ? scenario.note
      : "Ten scenarios, one per path the pipeline can take.";
    if (!scenario) return;
    $("user_id").value = scenario.user_id;
    $("subject").value = scenario.subject;
    $("body").value = scenario.body;
  });
}

function busy(button, running) {
  button.disabled = running;
}

function wire() {
  $("ticket-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.target.querySelector("button[type=submit]");
    const chosen = scenarios.find((s) => s.id === $("scenario").value);
    busy(button, true);
    try {
      await send(
        { user_id: $("user_id").value, subject: $("subject").value, body: $("body").value },
        chosen ? chosen.label : "Your ticket",
      );
    } catch (error) {
      showError(error);
    } finally {
      busy(button, false);
    }
  });

  $("seed").addEventListener("click", async (event) => {
    busy(event.target, true);
    try {
      for (const scenario of shuffled(scenarios)) {
        await send(
          { user_id: scenario.user_id, subject: scenario.subject, body: scenario.body },
          scenario.label,
        );
      }
    } catch (error) {
      showError(error);
    } finally {
      busy(event.target, false);
    }
  });

  $("lookup-go").addEventListener("click", async () => {
    const id = $("lookup").value.trim();
    if (!id) return;
    try {
      const ticket = await readTicket(id);
      if (!held.some((entry) => entry.id === id)) remember(id, "Opened by id");
      tickets.set(id, ticket);
      selected = id;
      $("lookup").value = "";
      render();
    } catch (error) {
      showError(error);
    }
  });

  $("forget").addEventListener("click", forgetAll);
}

function shuffled(list) {
  const copy = [...list];
  for (let i = copy.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [copy[i], copy[j]] = [copy[j], copy[i]];
  }
  return copy;
}

async function start() {
  wire();
  scenarios = await (await fetch("scenarios.json")).json();
  scenarioOptions();
  render();
  // An id whose ticket has gone -- a wiped database, someone else's id -- is dropped
  // rather than left showing as processing forever.
  for (const entry of held) {
    poll(entry.id).catch(() => forget(entry.id));
  }
}

start().catch(showError);
