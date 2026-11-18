# Mira — automated DME coordination (prototype)

Works Eleanor Martinez's standard manual wheelchair (K0001) case with no care advocate in the loop:
finds an enrolled supplier by phone, chases the PCP for a valid written order, hands off, follows up,
keeps the patient informed — and comes back to a human only when it has to.

Built on **Temporal Cloud** (durable workflows, timers, signals) with a **calling queue** of placeholder
voice agents. A **real LLM** (OpenRouter) reads every call transcript; plain code makes every decision.

See [WRITEUP.md](WRITEUP.md) for sequencing, architecture, cut list and what's next, and [plan.md](plan.md) for the build plan.

## Setup

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and the Temporal CLI with the cloud plugin
(`brew install temporalio/brew/temporal-cloud`). No local Temporal server, Docker or database.

```bash
uv sync
export OPENROUTER_API_KEY="your-openrouter-api-key"
```

Alternatively, create a local `.env` containing `OPENROUTER_API_KEY=your-openrouter-api-key`.
Environment files and local connection configuration are excluded from this public repository.

Temporal Cloud credentials live in the Temporal CLI profile `cloud-setup`, not in this repo
(`temporal cloud login`, then `temporal cloud apikey create-for-me` and
`temporal --profile cloud-setup config set --prop address|namespace|api_key`). Override with `TEMPORAL_PROFILE`.

| Env var | Default | Meaning |
|---|---|---|
| `OPENROUTER_API_KEY` | — | required |
| `OPENROUTER_MODEL` | `baidu/ernie-5.5-flash-preview:free` | any OpenRouter chat model |
| `TEMPORAL_PROFILE` | `cloud-setup` | Temporal CLI profile to connect with |
| `SECONDS_PER_DAY` | `5` | real seconds per simulated case day |
| `SCENARIO` | `default` | which `data/scenarios/*.json` the mocks replay |

## Run the demo

Terminal 1 — the worker (both task queues):

```bash
uv run python -m mira.worker
```

Terminal 2 — start Eleanor's case and wait for the summary (~1–2 minutes):

```bash
uv run python -m mira.start_case
```

It prints a Temporal Cloud UI link: every call, timer, child workflow and signal is in the history there.
The case log is in `runtime/events.jsonl`; escalations are in `runtime/escalations.json`.

**Default scenario** — what you should see: Lakeshore isn't taking Medicare patients, Halsted is out of stock,
Lincoln Park doesn't answer then says yes on the callback; the PCP office loses the request (re-faxed), then
sends an order coded K0003 (caught, fix-up call, corrected); Lincoln Park goes silent after handoff, so the
case falls back to Prairie State, which explicitly confirms a scheduled delivery. The patient's cost questions
become a non-blocking callback in the human inbox.

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
| **Review** (`HumanReviewWorkflow`, from Temporal) | no supplier confirmed, order stalled/needs info, no delivery confirmed, **technical failure** (LLM/provider/config errors that outlast retries) | paused | `decide retry` re-runs the step; `decide close` ends the case |
| **Callback** (from `runtime/escalations.json`) | patient unreachable, has questions, or unclear on cost/next steps | keeps going | `done`; completing it never touches or reopens the case |

**Try it:** restart the worker with `SCENARIO=escalation uv run python -m mira.worker` and run `start_case`.
About a minute in, the PCP office says Dr. Chen won't sign without a mobility assessment, and the case
waits for you. In the default scenario, patient questions create a non-blocking callback instead (the case
keeps going). Killing the worker mid-case and restarting it resumes the case where it left off; timers keep
running on wall-clock time while the worker is down, so a long outage can legitimately trip an SLA escalation.

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
| LLM extraction of every connected call (OpenRouter) | Fax out (order request, handoff) and fax in (signed orders from the scenario) |
| Supplier directory from `sample_dir.csv`; distance ranking | Patient phone and home ZIP (not in intake; placeholders in `data/case.json`) |
| Order consistency checks, qualification, escalation logic | Delivery itself, claim submission, eligibility and medical-necessity review |

## Layout

```
mira/
  workflows/  case.py (orchestrator) · sourcing.py · pcp_order.py · fulfillment.py · patient.py · human_review.py · common.py
  activities.py       place_call: voice agent → LLM extraction → CallOutcome  (voice-calls queue)
  case_activities.py  ranking, mock fax, escalations, event log               (dme-cases queue)
  policy.py           deterministic rules — where AI is deliberately kept out
  extraction.py, llm.py   transcript → structured outcome via OpenRouter
  voice/agent.py      VoiceAgent interface + MockVoiceAgent
  reviews.py          list/decide human reviews (used by human.py and start_case.py)
  worker.py · start_case.py · human.py · try_call.py
data/  case.json · scenarios/*.json · zip_centroids.json        sample_dir.csv (supplier directory)
```
