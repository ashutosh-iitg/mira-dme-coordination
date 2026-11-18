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
sends an order coded K0003 without an NPI (caught, fix-up call, corrected); Lincoln Park goes silent after
handoff, so the case falls back to Prairie State, which schedules delivery. The patient's cost questions become
a non-blocking callback for a human.

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
Use `--no-prompt` to never prompt. From any other terminal (works against Temporal, not local files):

```bash
uv run python -m mira.human list                                   # reviews waiting on Temporal
uv run python -m mira.human status <case-workflow-id>              # live case state (Temporal query)
uv run python -m mira.human decide <escalation-id> retry|close --note "..."
```

**Try it:** restart the worker with `SCENARIO=escalation uv run python -m mira.worker` and run `start_case`.
About a minute in, the PCP office says Dr. Chen won't sign without a mobility assessment, and the case
waits for you. In the default scenario, patient questions create a non-blocking callback instead (the case
keeps going). Killing the worker mid-case and restarting it resumes the case where it left off; timers keep
running on wall-clock time while the worker is down, so a long outage can legitimately trip an SLA escalation.

## Tests

```bash
uv run pytest
```

Unit tests cover policy, ranking, data loading, extraction routing and the call activity. Workflow tests run the
full case (default, all-silent, escalation scenarios) on Temporal's time-skipping test server with a fake LLM;
the test server binary is downloaded on first run.

## What's real and what's mocked

| Real | Mocked |
|---|---|
| Temporal Cloud workflows, durable timers, retries, signals, queries | Phone calls: `MockVoiceAgent` replays scripted transcripts per party/purpose/attempt |
| LLM extraction of every connected call (OpenRouter) | Fax out (order request, handoff) and fax in (signed orders from the scenario) |
| Supplier directory from `sample_dir.csv`; distance ranking | Patient phone and home ZIP (not in intake; placeholders in `data/case.json`) |
| Order validation, qualification, escalation logic | Delivery itself, claim submission, eligibility checks |

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
