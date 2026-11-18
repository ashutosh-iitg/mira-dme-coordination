# Mira — automated DME coordination (prototype)

Works Eleanor Martinez's standard manual wheelchair (K0001) case with no care advocate in the loop:
finds an enrolled supplier by phone, chases the PCP for a valid written order, hands off, follows up,
keeps the patient informed — and comes back to a human only when it has to.

Built on **Temporal Cloud** (durable workflows, timers, signals) with a **calling queue** of placeholder
voice agents. A **real LLM** (Groq or OpenRouter) extracts call facts. The additional coordinator proposes
administrative actions; application code validates business-state transitions.

See [WRITEUP.md](WRITEUP.md) for sequencing, architecture, cut list and the ordered product roadmap;
[ARCHITECTURE.md](ARCHITECTURE.md) expands the diagrams and safety/consistency decisions. [plan.md](plan.md)
records implementation progress.

The additional [Deep Agents coordinator demo](COORDINATOR.md) supervises PT/report and cost follow-ups on
Temporal, with JSON case documents and mocked communication/booking. Run its separate worker with
`uv run python -m mira.coordinator.worker`; use `uv run python -m mira.coordinator --help` for its CLI.

The real case uses that coordinator for supplier requirements, administrative PCP information requests,
and patient cost questions. See [MIDRUN_COORDINATION.md](MIDRUN_COORDINATION.md) for the sequence diagram,
evidence graph and safeguards. Run the normal worker and `start_case`; use `SCENARIO=pcp_information`
or `SCENARIO=supplier_information` on the worker to focus on a particular loop.

## Setup

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and the Temporal CLI with the cloud plugin
(`brew install temporalio/brew/temporal-cloud`) for the Cloud demos. Workflow tests instead
start temporary local time-skipping Temporal servers through the SDK; their histories do not appear in Cloud.
No database server or Docker is required.

```bash
uv sync
export MODEL_PROVIDER=groq
export GROQ_API_KEY="your-groq-api-key"
export GROQ_MODEL="openai/gpt-oss-120b"
```

Alternatively, put these variables in a local `.env`. To use OpenRouter, set `MODEL_PROVIDER=openrouter`
and provide `OPENROUTER_API_KEY` and optionally `OPENROUTER_MODEL`.
Environment files and local connection configuration are excluded from this public repository.

Temporal Cloud credentials live in the Temporal CLI profile `cloud-setup`, not in this repo
(`temporal cloud login`, then `temporal cloud apikey create-for-me` and
`temporal --profile cloud-setup config set --prop address|namespace|api_key`). Override with `TEMPORAL_PROFILE`.

| Env var | Default | Meaning |
|---|---|---|
| `MODEL_PROVIDER` | `openrouter` | `groq` or `openrouter`; applies to extraction and coordinator; local demo uses `groq` |
| `GROQ_API_KEY` | — | required when provider is `groq` |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Groq model ID |
| `OPENROUTER_API_KEY` | — | required when provider is `openrouter` |
| `OPENROUTER_MODEL` | `baidu/ernie-5.5-flash-preview:free` | OpenRouter model ID |
| `COORDINATOR_MODEL` | selected provider's model | optional coordinator-only model override within that provider |
| `TEMPORAL_PROFILE` | `cloud-setup` | Temporal CLI profile to connect with |
| `SECONDS_PER_DAY` | `5` | real seconds per simulated case day |
| `SCENARIO` | `default` | which `data/scenarios/*.json` the mocks replay |

## Run the demo

Terminal 1 — the worker (both task queues):

```bash
uv run python -m mira.worker
```

Terminal 2 — start Eleanor's case and wait for the summary (runtime depends on model latency, retries and quota):

```bash
uv run python -m mira.start_case
```

It prints a Temporal Cloud UI link: every call, timer, child workflow and signal is in the history there.
The case log is in `runtime/events.jsonl`; escalations are in `runtime/escalations.json`.

**Default scenario** — what you should see: Lakeshore isn't taking Medicare patients, Halsted is out of stock,
Lincoln Park doesn't answer then says yes on the callback; the PCP office loses the request (re-faxed), then
sends an order coded K0003 (caught, fix-up call, corrected); Lincoln Park goes silent after handoff, so the
case falls back to Prairie State, which explicitly confirms a scheduled delivery. Eleanor's cost and folding
questions go to the case coordinator, which asks the supplier, relays the answer and asks her to confirm it; the case
keeps going meanwhile. Before finishing, it waits for questions to settle or creates callbacks. If she does not explicitly accept the answer, the question becomes a callback in the human inbox.

| `SCENARIO` | What it shows |
|---|---|
| `default` | All the supplier/PCP friction above, plus the patient's questions answered by the coordinator |
| `pcp_information` | Dr. Chen will sign once she has a copy of an existing PT evaluation: the order chase pauses, the coordinator gets it from the patient (with her permission), the office accepts it, the chase resumes |
| `supplier_information` | A supplier needs a mobility assessment report before taking the case: patient → PCP → patient permission → supplier acceptance |
| `escalation` | Dr. Chen wants a *new* mobility assessment: clinical work, so it goes straight to a human review |

### When the case needs a human

A blocking escalation starts a **`HumanReviewWorkflow`** child (ID `<escalation-id>/human-review`). In the
Temporal Cloud dashboard it shows as a *Running* workflow with the summary "Human review: <reason>" and live
details explaining what's needed. The case workflow sits on it until a decision arrives.

`start_case` follows the case live (prints each stage change) and, when a review opens, prints a
**⏸ CASE IS WAITING FOR A HUMAN** banner and prompts in the same terminal:

```
  Decide now? [r]etry / [c]lose / [s]kip (decide later from another terminal):
```

`retry` re-runs the step that escalated; `close` ends the case with your note; `skip` leaves it waiting.
Use `--no-prompt` to never prompt. From any other terminal:

```bash
uv run python -m mira.human list                                   # everything that needs a person
uv run python -m mira.human status <case-workflow-id>              # live case state (Temporal query)
uv run python -m mira.human decide <escalation-id> retry|close --note "..."   # blocking review
uv run python -m mira.human done <escalation-id> --note "..."                 # callback
```

The inbox has two kinds of item:

| Kind | Raised when | Case | Resolve with |
|---|---|---|---|
| **Review** (`HumanReviewWorkflow`, from Temporal) | no supplier confirmed, order stalled, a supplier/PCP request that is clinical or the coordinator could not clear, no delivery confirmed, **technical failure** (LLM/provider/config errors that outlast retries) | paused | `decide retry` re-runs the step; `decide close` ends the case |
| **Callback** (from `runtime/escalations.json`) | patient unreachable, unclear on cost/next steps, or her questions were not answered by the coordinator | keeps going | `done`; completing it never touches or reopens the case |

Administrative requests (a copy of an existing report, a cost estimate) are worked by the coordinator first; only
what it cannot clear, or anything needing clinical judgment, reaches this inbox.

**Try it:** restart the worker with `SCENARIO=escalation uv run python -m mira.worker` and run `start_case`.
About a minute in, the PCP office says Dr. Chen won't sign without a mobility assessment, and the case
waits for you. Killing the worker mid-case and restarting it resumes the case where it left off; timers keep
running on wall-clock time while the worker is down, so a long outage can legitimately trip an SLA escalation.

**Model rate limits.** A free-tier model can run out of daily quota mid-case. Calls that still fail after
retries become a *technical failure* review (e.g. `…:technical-pcp-order`); the case waits safely. Once the
quota resets, `decide <escalation-id> retry` resumes that step. Coordination adds model turns, so budget for dozens of requests and allow for retries; the earlier 30–40-request estimate is not a quota guarantee for the current default scenario.

**Verification status:** 99 credential-free tests pass. Saved live evidence confirms the PCP loop and a default run with safe patient-callback fallback. The latest patient-acceptance prompt has not been rerun live after daily quota exhaustion. The written-order filter also has a known compound-request limitation. See [the current review](ARCHITECTURE.md#current-review-and-verification).

## Tests

```bash
uv run pytest
```

Unit tests cover policy (screening, explicit-scheduling follow-ups, order consistency), ranking, the escalation
store and callback lifecycle, data loading, extraction routing and the call activity. Workflow tests run on
Temporal's time-skipping test server with a fake LLM: the default case end to end, all-silent and needs-info
escalations closed by a human, a forced LLM-provider outage recovered by a human `retry`, and the PCP office's
first answer acted on immediately. The fake LLM tests orchestration, not extraction quality. The test server
binary is downloaded on first run.

The full suite currently has **99 tests**, including provider routing, coordinator lifecycle, evaluator scoring,
real-case supplier/PCP/patient coordination, clinical gates, fallback reviews that target the right task, and
contact/action budgets. Historical live model results
are documented separately in [COORDINATOR_EVAL_RESULTS.md](COORDINATOR_EVAL_RESULTS.md) and
[SIMULATION_RESULTS.md](SIMULATION_RESULTS.md).

## What the order checks do and don't mean

`validate_order` checks the written order is consistent with this case: CMS standard-written-order elements
(beneficiary, order date, item description, quantity, practitioner name **or** NPI, signature) and an HCPCS match
with the K0001 being delivered. Passing it is **not** a coverage determination: medical necessity (mobility
limitation, why a cane or walker isn't enough, use at home) is requested from the PCP and left to the PCP and
supplier. Eleanor's ~20% share is quoted as conditional on the supplier accepting Medicare assignment, which
screening asks about. The case ends at a delivery the supplier **explicitly confirmed as scheduled**: typical
timing ("usually four days") does not count.

## What's real and what's mocked

| Real | Mocked |
|---|---|
| Temporal Cloud workflows, durable timers, retries, signals, queries | Phone calls: `MockVoiceAgent` replays scripted transcripts per party/purpose/attempt |
| LLM extraction of every connected call (selected model provider) | Fax out (order request, handoff) and fax in (signed orders from the scenario) |
| Supplier directory from `sample_dir.csv`; distance ranking | Patient phone and home ZIP (not in intake; placeholders in `data/case.json`) |
| Order consistency checks, qualification, escalation logic | Delivery itself, claim submission, eligibility and medical-necessity review |

## Layout

```
mira/
  workflows/  case.py (orchestrator) · sourcing.py · pcp_order.py · fulfillment.py · patient.py · human_review.py · common.py
              requirement_gate.py  pause a step until the coordinator clears a supplier/PCP/patient requirement
  coordinator/  workflow.py · agent.py (Deep Agents planner) · store.py (JSON case document + transition checks)
                knowledge.py (requirement evidence graph) · communication.py (coordinator calls)
  activities.py       place_call: voice agent → LLM extraction → CallOutcome  (voice-calls queue)
  case_activities.py  ranking, mock fax, escalations, event log               (dme-cases queue)
  policy.py           deterministic rules — where AI is deliberately kept out
  extraction.py, llm.py   transcript → structured outcome via Groq or OpenRouter
  voice/agent.py      VoiceAgent interface + MockVoiceAgent
  reviews.py          list/decide human reviews (used by human.py and start_case.py)
  worker.py · start_case.py · human.py · try_call.py
data/  case.json · scenarios/*.json · zip_centroids.json        sample_dir.csv (supplier directory)
```
