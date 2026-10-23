# RoboGraph

An evidence-driven recovery agent for a small robot fleet. Pick a robot with a problem and RoboGraph checks FalkorDB for incidents, recoveries, signal history and missions, shows the evidence for and against each option, lets an LLM agent record a decision that the graph has to support, and keeps that decision, its approval and its outcome in the same graph.

Built for Graph Hacks: Context for AI Agents (WeMakeDevs + FalkorDB).

## What it does

- **Evidence check.** For the selected robot, Cypher queries find what the history supports: which adjacent locations earlier robots recovered by moving to, how strong the signal is there, what failed there, and what is missing. Each option gets a verdict (`supported`, `weak`, `conflicting`, `rejected`), a one-line reason, evidence for and against, and missing information. The Cypher that ran is shown in the UI. The check runs without the model and is the source of truth for the agent, the validation rules and the UI.
- **Agent.** An LLM records one decision with `record_decision`. The graph has to accept it: the action must fit the problem, a reroute destination must be adjacent, `supported`, and backed by cited incidents that recovered that way. Rejections go back to the model as tool errors that name the supported destinations, and it gets a bounded number of corrections.
- **Fallback.** If the agent fails (model error, timeout, no decision, repeated rejections), the backend records the evidence engine's recommendation, or `request_review` with the specific reason when nothing is supported. It only does this if the agent has not already recorded a decision, it never duplicates an identical unreviewed decision, and the result is labelled as recorded by the backend.
- **Decision lifecycle.** The evidence engine and the agent *propose*. A recorded decision starts as `recorded`, then an operator marks it `approved` or `rejected`, then `executed` (reported by the operator after they carried it out), then reports the outcome (`recovered` / `not_recovered`). RoboGraph never moves a robot. Only verified outcomes count as evidence next time, and a stored recommendation is never treated as a successful recovery.
- **Counterfactual.** Copies the graph (`GRAPH.COPY`), changes one thing, re-runs the checks, and reports which options changed verdict and exactly which evidence was removed, added or contradicted, with the outcome `unchanged`, `changed`, `uncertain` or `failed`. Optionally runs the agent on the copy to see whether it independently reaches a valid decision. The original graph is verified unchanged and the copy is always deleted.
- **Evaluation.** Nine fixed scenarios, each on its own graph copy: recovery history, motor fault, healthy robot, missing evidence, conflicting evidence, two counterfactuals, and two attempts to record unsupported decisions.

## Why a graph

The answer is not in one record. It takes a chain: robot, current location, other incidents at that location, which robots had them, where those robots moved to recover, how that place performs, whether it failed for someone else, and whether it is adjacent. Each step is a traversal over the same connected data, and the result (decision, cited incidents, outcome) is written back so the next investigation starts with it.

## Architecture

```
browser ── FastAPI ──┬── agent loop ── LLM (OpenAI-compatible, tool calling)
                     │        │
                     │        └── tools ──┬── assess.py  evidence engine (Cypher + verdict rules)
                     │                    └── tools.py   queries, record_decision validation, approvals
                     ├── experiments.py   counterfactual and evaluation on graph copies
                     └── FalkorDB
```

| File | Role |
|---|---|
| `app/db.py` | FalkorDB connection, `query`, active-graph switching for copies |
| `app/core.py` | id validation, thresholds, shared lookups |
| `app/assess.py` | evidence engine: the Cypher and the verdict rules |
| `app/tools.py` | agent tools, `record_decision` guards, approval/outcome, trace, robot and telemetry ingestion |
| `app/agent.py` | tool-calling loop with time budget, bounded steps, duplicate-call detection |
| `app/experiments.py` | counterfactual changes and evaluation scenarios |
| `app/seed.py` | deterministic demo graph |
| `app/main.py` | API, limits, error handling; `app/static/` is the UI |

I used direct tool calling rather than MCP: one client, one fixed tool set, so an MCP server would add a process boundary without changing what the agent can do.

## Graph model

```
(Robot)-[:LOCATED_AT]->(Location)-[:CONNECTS_TO]-(Location)
(Robot)-[:GENERATED]->(Telemetry)-[:RECORDED_AT]->(Location)
(Robot)-[:EXECUTED]->(Mission)-[:PASSED_THROUGH {seq}]->(Location)
(Robot)-[:EXPERIENCED]->(Incident)-[:OCCURRED_AT]->(Location)
(Incident)-[:INVOLVED_COMPONENT]->(Component)
(Incident)-[:RESOLVED_BY]->(Engineer)
(Incident)-[:RESOLVED_VIA]->(Location)        only when moving the robot fixed it
(Robot)-[:HAS_NOTE]->(Observation)
(Decision)-[:ABOUT]->(Robot)
(Decision)-[:SUPPORTED_BY]->(Incident)
(Decision)-[:RECOMMENDS]->(Location)
```

`Decision` properties: `action`, `summary`, `issue_type`, `from_location`, `source` (`agent` or `engine_fallback`), `status` (`recorded`, `approved`, `rejected`, `executed`), `outcome` (`recovered`, `not_recovered`, absent until verified). Decisions saved as `proposed` by an earlier version are migrated to `recorded` on startup.

Seed data: 4 robots, 9 locations, 32 telemetry readings, 5 missions, 7 incidents, 2 notes.

- **R4, communication loss at W5.** R1 and R2 recovered from W5 by moving to W7. W6 is also adjacent and on R4's planned route, but R3's detour through it failed. R4's own earlier incidents were only patched by radio restarts.
- **R3, motor overheating.** Earlier motor incidents recovered after servicing, so rerouting is not an option.

## Evidence rules

For each location adjacent to a robot with a communication problem:

| Verdict | When |
|---|---|
| `supported` | at least one incident that started at the robot's location recovered by moving here, average RSSI is -70 or better, and nothing failed here |
| `conflicting` | recoveries exist and so do failures here (a failed incident, or an approved reroute verified as not recovered) |
| `rejected` | failures here and no recoveries |
| `weak` | not enough: only the signal looks good, or recoveries exist but the signal is poor, or there is no evidence either way. `missing` information says which |

Only recovered incidents can justify a recommendation. Verified agent outcomes can count against an option and are listed as supporting evidence when they succeed, but a decision cannot justify itself. Existence of an adjacent location is never treated as evidence that it is safe. If nothing is `supported`, the answer is `request_review` with the reason for each option. For motor overheating the same rules apply to servicing, based on earlier motor incidents and their outcomes. Removing records (counterfactual) lowers an option to `weak`, never to `rejected`, because missing data is not evidence of failure.

## How the agent and the guards work

To keep a small local model reliable, the backend runs `get_robot_context`, `get_robot_memory` and `assess_incident` first and shows them to the model as tool results (the response marks them `prefetched`). The model is offered `record_decision`, `find_similar_incidents` and `get_location_signal`. `assess_incident` returns ready-to-use `record_decision_args`. Repeating a read-only call is served from a per-investigation cache.

`record_decision` rejects: an action that does not fit the problem, incidents of the wrong type, a missing or non-adjacent destination, a destination that is not `supported`, a reroute without a cited incident that recovered that way, and a second decision in the same investigation. Every robot-scoped tool call is checked against the robot being investigated; a call for another robot is rejected, and a missing `robot_id` is filled with the current one.

Bounds: at most 6 model calls, 2 rejected `record_decision` attempts, a total time budget (`INVESTIGATION_BUDGET_S`), a per-call timeout (`LLM_TIMEOUT`) with no automatic retries, and a stop after two turns that only repeat earlier calls. The loop also stops as soon as a decision is recorded, so there is no extra model call just to summarise. The agent's explanation is the summary it recorded.

Latency: the graph side costs about 15 ms per investigation (measured: context 2.5 ms, memory 0.8 ms, assessment 2.9 ms, trace 8 ms). Time goes to model calls, each of which re-reads the prompt (about 7,000 characters before this change, system prompt plus tool schemas plus results). The changes cut the number of sequential model calls from four or five to one in the normal case, trim the offered tool schemas from about 2,500 to 1,500 characters, and cap generation (`LLM_MAX_TOKENS`) at a low temperature (`LLM_TEMPERATURE`). Every response includes per-call model and tool timings and prompt sizes. I could not measure `qwen2.5:3b` itself here, so check the timings in the UI on your hardware.

## Decision trace

Built from the stored `Decision` node and its edges, plus the alternatives the evidence check did not choose:

```
R4 at W5                      RSSI -88 dBm, motor 43 C
W5 history                    avg RSSI -84.5 dBm over 13 readings from R1, R2, R4; 4 comm_loss
INC-101: R2 comm_loss at W5   recovered via W7, Priya Nair. Antenna inspected, no fault. Rerouted via W7, link recovered.
INC-102: R1 comm_loss at W5   recovered via W7, Arjun Rao. Rerouted via W7, link stable through W8.
W7 history                    avg RSSI -53.4 dBm over 5 readings; 0 incident(s); 2 completed mission(s) passed through
Not chosen: W4 (weak)         signal at W4 looks good, but no incident has recovered by moving there; 1 aborted mission(s) ...
Not chosen: W6 (rejected)     failures are recorded at W6 and no incident recovered by moving there; INC-103 (R3) had comm_loss at W6 and ended failed; avg RSSI -79.7 dBm (below -70); ...
Recommend: reroute W5 to W7   <the agent's summary>
```

## Key Cypher

Recoveries from the robot's current location (`recoveries_from_here`):

```cypher
MATCH (r:Robot)-[:EXPERIENCED]->(i:Incident {type: 'comm_loss'})-[:OCCURRED_AT]->(:Location {id: $here}),
      (i)-[:RESOLVED_VIA]->(n:Location)-[:CONNECTS_TO]-(:Location {id: $here})
RETURN n.id AS id, i.id AS incident, r.id AS robot
```

Failures at adjacent candidates (`failed_incidents_at_candidates`):

```cypher
MATCH (:Location {id: $here})-[:CONNECTS_TO]-(n:Location)<-[:OCCURRED_AT]-(i:Incident {type: 'comm_loss'})
WHERE i.outcome <> 'recovered'
OPTIONAL MATCH (r:Robot)-[:EXPERIENCED]->(i)
RETURN n.id AS id, i.id AS incident, r.id AS robot, i.outcome AS outcome
```

The rest are in `app/assess.py`. All queries are fixed strings with parameters.

## Tech stack

Python 3.12, FastAPI, FalkorDB (`falkordb` client), OpenAI Python SDK (chat completions with tools; works with any OpenAI-compatible server), plain JS frontend, pytest.

## Setup

Requires Python 3.12 (the version it was tested on), Docker for FalkorDB, and an OpenAI-compatible endpoint.

```
docker compose up -d
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
cp .env.example .env                               # Windows: copy .env.example .env
```

### Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `OPENAI_API_KEY` | model access, server side only | required (any value for local servers) |
| `OPENAI_MODEL` | model with tool calling | `gpt-4o-mini` |
| `OPENAI_BASE_URL` | OpenAI-compatible endpoint | OpenAI |
| `LLM_TIMEOUT` | seconds per model call, no retries | `120` |
| `LLM_MAX_TOKENS` | cap on generated tokens per call; `0` leaves it unset | `400` |
| `LLM_TEMPERATURE` | sampling temperature; empty leaves it unset | `0` |
| `INVESTIGATION_BUDGET_S` | total time budget per investigation | `180` |
| `FALKORDB_HOST`, `FALKORDB_PORT` | FalkorDB location | `localhost`, `6379` |
| `FALKORDB_PASSWORD` | if FalkorDB requires one | none |
| `FALKORDB_GRAPH` | graph name | `robograph` |

### Local models (Ollama)

Use a model with tool calling (`qwen2.5:3b`, `qwen2.5:7b`, `llama3.1:8b`) and set `OPENAI_BASE_URL=http://localhost:11434/v1`, `OPENAI_API_KEY=ollama`, `OPENAI_MODEL=qwen2.5:3b`. Raise `LLM_TIMEOUT` and `INVESTIGATION_BUDGET_S` on slow hardware, check `ollama ps` shows the GPU, and give the model enough context (`OLLAMA_CONTEXT_LENGTH=8192`). A 3B model sometimes sends bad arguments or stops without recording a decision; the tool errors, the bounded corrections and the backend fallback exist for that, and the result always says whether the agent completed. If an OpenAI-compatible server rejects `max_tokens` or `temperature`, set the variable to `0` or empty. The evidence check and the counterfactual (without the agent) do not use the model and respond immediately.

## How to run

```
python -m uvicorn app.main:app --env-file .env
```

Open http://127.0.0.1:8000. The demo graph is created on first start. `python -m app.seed` wipes decisions and notes and reloads the demo data (it reads variables from your shell).

## Connecting more robots

```
curl -X POST localhost:8000/api/robots -H 'content-type: application/json' \
  -d '{"id":"R5","name":"Hauler 5","model":"Rover-A","location_id":"W3"}'

curl -X POST localhost:8000/api/telemetry -H 'content-type: application/json' \
  -d '{"robot_id":"R5","location_id":"W4","rssi":-91,"battery":70,"temp_c":41}'
```

A telemetry post stores a reading and moves the robot to `location_id`. Values are range-checked (RSSI -120 to 0, battery 0 to 100, temperature -20 to 150); ids must match `R<number>` and an existing location. A ROS 2 node or an ESP32 can post these. New locations and connections are not an API yet; add them in `app/seed.py`.

## How to test

```
python -m pytest
```

Needs FalkorDB running. Tests use a separate graph (`robograph_test`) and reseed it for each test. They cover the verdicts and their reasons, every `record_decision` rejection, injection attempts, the full investigation and trace, the decision lifecycle, memory across investigations, verified outcomes counting only after verification, counterfactual outcomes and evidence diffs, failed simulations, copy cleanup after exceptions, immutability of the original graph, evaluation mode, robot and telemetry ingestion, DB down, LLM missing or failing, rate and size limits.

The tests do not call a real model. Mocked model responses drive the agent loop, and the scripted stand-in picks tool calls from the tool results it receives (it has no robot-specific answers). Agent tests cover a missing or unsupported destination followed by a correction, repeated rejection, finishing without a decision, fallback to the supported option and to `request_review`, duplicate prevention, cross-robot calls, timeouts and model errors. Whether a real model investigates well depends on the model you configure.

## Demo (about three minutes)

1. Open R4. The evidence check shows W7 supported (INC-101, INC-102), W4 weak, and W6 rejected because of INC-103, each with a reason. Expand the Cypher.
2. Click **Investigate R4**. The AI agent result and the recorded decision are shown separately, with the trace, why W6 and W4 were not chosen, and per-call timings.
3. Approve it, mark it executed (you carried it out), then mark it recovered. The memory panel and the W7 evidence now show the verified outcome.
4. In **Counterfactual**, remove the recovery evidence for W7. The recommendation becomes uncertain and the panel shows exactly which two incidents stopped supporting W7. Tick "also run the agent" to see whether it reaches the same valid decision.
5. Click **Run evaluation** to show all scenarios, including the two refused decisions.

## Security considerations

- The browser never sees the API key or database credentials. `/api/health` reports only whether a key is set.
- The client cannot run Cypher. Queries are fixed strings with parameters, counterfactual changes are a fixed list, tool arguments are validated, and unknown arguments are rejected.
- Graph content, including operator notes, reaches the model as tool output, labelled untrusted. Notes are stripped of control characters and capped at 300 characters. Prompt injection cannot be ruled out for any LLM, so the important constraints are enforced in code (see the guards above).
- Request bodies over 4 KB are rejected, fields have length and range limits, heavy requests (investigate, counterfactual, evaluate) are limited to 10 per minute per client address, and the agent is bounded by steps and time. The fleet is capped at 50 robots.
- No CORS headers are sent. Responses carry `Content-Security-Policy: default-src 'self'` and `nosniff`. Interactive API docs are disabled.
- There is no authentication, and the robot, telemetry, approval, execution, outcome and note endpoints write to the graph. Run it on localhost (the default) or put it behind something that authenticates. The rate limiter is keyed by client address, so behind a proxy it needs forwarded-address handling.
- RoboGraph recommends and records; it does not command robots. Approval, execution and outcome are reported by the operator.

## Hackathon track

Primary: **Track 01, agents that act on connected data.** The recommendation comes from traversing incidents, locations, robots, missions and telemetry, and the action is written back into the same graph.

Also **Track 02, memory and coordination.** Decisions, approvals and verified outcomes persist in the graph, and one robot's incident history decides another robot's recovery.

## AI assistance

This project was built with assistance from Claude (Anthropic) for code, tests and documentation.
