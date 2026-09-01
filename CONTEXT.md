# Support Ticket Auto-Reply

A service that answers customer support tickets for an EV-charging app automatically, or
hands them to a human when it cannot answer them safely.

This file is the project's vocabulary and nothing else. How each concept is built is in
the component documentation; why it is built that way is in [docs/adr/](docs/adr/).

## Language

### The customer's world

**Ticket**:
A support request from a user, and the record of what the service did about it.
_Avoid_: case, issue, enquiry

**User**:
A person who uses the EV-charging app and can raise a ticket.
_Avoid_: customer, client, account

**Charging session**:
One use of a charging station by a user.
_Avoid_: charge, top-up, visit

**Invoice**:
A request for payment issued to a user.
_Avoid_: bill, receipt, statement

**Reply**:
The message sent back to the user. A ticket has at most one, and only ever an automatic
one.
_Avoid_: response, answer, resolution

### The pipeline

**Pipeline**:
The automated sequence that takes one ticket from arrival to a final outcome.
_Avoid_: workflow, chain, flow

**Orchestrator**:
The component that runs the pipeline and is the sole decider of a ticket's outcome.
_Avoid_: runner, controller, engine, coordinator

**Intent**:
The kind of question a ticket is asking, as classified by the pipeline.
_Avoid_: category, class, topic, type

**Classification**:
The intent the pipeline decided a ticket has, together with the evidence for that
decision.
_Avoid_: categorisation, label, prediction

**Step**:
A single action the model chooses: gather more data, reply, or give up.
_Avoid_: action, move, instruction

**Iteration**:
One pass of the tool loop — the model choosing a step and the system carrying it out.
_Avoid_: turn, cycle, round

**Observation**:
A tool call together with what it returned, forming part of what the model sees next.
_Avoid_: memory, context, outcome

**History**:
The observations gathered so far in one pipeline run.
_Avoid_: context, transcript, state

### Gathering data

**Tool**:
One of the fixed set of functions through which the pipeline may obtain user data. The
pipeline has no other route to data.
_Avoid_: function, capability, integration, skill

**Tool result**:
What a tool returned for one call.
_Avoid_: payload, response, output

**Fixture**:
The local data the tools read, standing in for the production data source.
_Avoid_: seed data, mock data, sample data

### Safety

**Fact**:
A single value that provably came from a tool result.
_Avoid_: datum, value, field

**Fact set**:
The complete collection of facts available to a reply, and the only material it may be
built from.
_Avoid_: context, evidence, knowledge base

**Grounding**:
The property that every factual claim in a reply traces back to a tool result.
_Avoid_: sourcing, attribution, citation

**Handoff**:
The outcome in which no reply is sent and a human takes the ticket over.
_Avoid_: escalation, transfer, fallback, deferral

**Handoff reason**:
The recorded explanation of why a particular ticket was handed off. A handoff always has
exactly one.
_Avoid_: error, cause, failure

**Fail closed**:
The principle that an uncertain pipeline hands off rather than replies, because a wrong
reply costs more than a slow one.
_Avoid_: fail safe, graceful degradation, best effort

### Audit

**Trace**:
The ordered record of everything the pipeline did for one ticket, kept so that a support
agent can explain the outcome afterwards.
_Avoid_: audit log, log, history

**Trace step**:
One entry in a trace. Distinct from a *step*: a step is what the model chose, a trace step
is what the system recorded.
_Avoid_: event, entry, record
