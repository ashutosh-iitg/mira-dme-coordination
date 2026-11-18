# Mira DME Coordination — Build Plan (3h time-box)

Goal: Eleanor Martinez's K0001 wheelchair case gets worked end to end with no care advocate in the loop,
coming back to a human only when it genuinely has to.

Focus areas:
1. **Durable orchestration** of the case (Temporal workflows, timers, signals, retries).
2. **Calling queue** for outbound calls with placeholder voice agents.

## Decisions

| Area | Choice | Why |
|---|---|---|
| Orchestration | Temporal **Cloud** (no local server) | Durable timers for "call back tomorrow", retries, signals for inbound events, history UI for the demo |
| Temporal auth | CLI profile `cloud-setup` (temporal.toml) via `temporalio.envconfig` | API key never lives in the repo |
| Language | Python 3.13, `uv` | Temporal Python SDK + Pydantic |
| LLM | OpenRouter (`baidu/ernie-5.5-flash-preview:free`) through the `openai` SDK directly, no framework | Real LLM calls required; schema in prompt + `response_format` when supported, validated with Pydantic |
| Voice | `VoiceAgent` interface + `MockVoiceAgent` replaying scripted transcripts | Phone calls mocked; real provider slots in behind the interface |
| Data | `sample_dir.csv`, `data/case.json`, `data/scenarios/*.json`, `runtime/events.jsonl`, `runtime/escalations.json` | No DB, no auth |
| Time | `SECONDS_PER_DAY` scales simulated days to real seconds | A week-long case demos in ~2 minutes on real Temporal timers |

**Where AI sits:** reading call transcripts into structured outcomes (with confidence).
**Where AI is deliberately kept out:** supplier qualification, order validation (code/fields), retry/fallback/escalation
decisions — deterministic code in `mira/policy.py` and the workflows.

## Architecture

```
CaseWorkflow (one per case)
 ├─ SupplierSourcingWorkflow  rank by distance → call in order → no-answer callbacks on durable timers → primary + backup
 ├─ PcpOrderWorkflow          request order → nudge on timers → resend if lost → validate → fix-up call → escalate on SLA
 ├─ Match & handoff           (phase 4) code match, delivery scheduling, silent-supplier fallback to backup
 └─ Patient updates           (phase 4) milestone calls, cost-share explanation, unreachable → escalate

Task queues:  dme-cases (workflows + case activities)   voice-calls (place_call, capped at PHONE_LINES)
place_call:   MockVoiceAgent → transcript → LLM extraction → CallOutcome
```

## Phases & status

### Phase 0 — Setup (0:00–0:15) ✅
- [x] uv project, deps (temporalio, openai, pydantic, python-dotenv), `.env.example`
- [x] Temporal Cloud connected using a local CLI profile (connection details excluded from this public repo)
- [x] Pydantic models; CSV + case loaders with validation

### Phase 1 — Call queue + voice placeholder (0:15–1:00) ✅
- [x] `MockVoiceAgent` keyed by phone/purpose/attempt (deterministic under retries)
- [x] `place_call` activity: mock call → LLM extraction → `CallOutcome`; config errors non-retryable
- [x] `voice-calls` task queue with concurrency cap; JSONL event log
- [x] `try_call` CLI for one-off calls without Temporal
- [x] Verify extraction against a real OpenRouter model

### Phase 2 — Supplier sourcing workflow (1:00–1:35) ✅
- [x] Rank suppliers by distance from patient ZIP (public ZIP centroids; no invented supplier data)
- [x] Call in order; deterministic qualification; low-confidence extraction treated as "call again"
- [x] No-answer → durable timer → callback round (max attempts per supplier)
- [x] Return primary + backup (shortest ETA, then distance); none qualified → escalate

### Phase 3 — PCP order workflow (1:35–2:05) ✅
- [x] Request written order; nudge timer; "never got it" → resend by fax (mock) and restart clock
- [x] Order arrives (`order_received` signal or mock fax inbox) → deterministic validation (HCPCS code, SWO elements, signature)
- [x] Invalid order → immediate fix-up call with the specific problems
- [x] SLA exceeded / max nudges / needs info → escalate (idempotent, `runtime/escalations.json`)
- [x] Workflow tests on Temporal's time-skipping test server with a fake LLM (default + all-silent scenarios)
- [x] First live run on Temporal Cloud (`mira.worker` + `mira.start_case`) with the real model — matches the test outcome

### Phase 4 — Match, handoff, patient updates, escalation (2:05–2:35) ✅
- [x] Run sourcing + order in parallel; billing-code check before handoff
- [x] Silent supplier: check-in timer → follow-up calls → fall back to backup; exhausted → escalate, `retry` re-sources excluding tried suppliers
- [x] Patient milestone calls (plan, delivery) with cost share; unreachable or open questions → non-blocking callback
- [x] Blocking escalations start a visible `HumanReviewWorkflow` child and wait for its `decide` signal (`retry`/`close`)
- [x] `start_case` follows the case live, prints a WAITING FOR HUMAN banner and prompts inline; `mira.human list|status|decide` from any terminal
- [x] Verified live: worker killed mid-case, case resumed from Temporal history; human `retry` resumed a stalled order chase

### Phase 5 — Demo + docs (2:35–3:00) ✅
- [x] `start_case` demo (default scenario) + `SCENARIO=escalation` human-in-the-loop scenario
- [x] README (run steps, human console, what's mocked) and WRITEUP (sequencing, architecture, cut list, what's next)
- [x] 28 tests: unit + full-case workflow tests on Temporal's time-skipping server

## Review follow-ups (REVIEW.md), done in its priority order ✅
- [x] 1. Delivery needs scheduling evidence: separate follow-up extraction (`order_received`, `delivery_status`); only explicit `scheduled` completes; tentative ETAs retry; non-negative ETAs; never-confirmed is `unconfirmed`, not `silent`
- [x] 4. Coverage: service area must be affirmatively confirmed; no distance cutoff (distance only orders calls); same-day ETA (0) ranks first
- [x] 2. Callbacks in the inbox: `mira.human list` merges Temporal reviews + open callbacks; `done` completes a callback without touching the case; atomic escalation store
- [x] 3. Technical failures: child-step failures become blocking technical reviews (step + error category); `retry` re-runs the step; patient-call failures become callbacks
- [x] 5. First PCP answer drives the next action (signed → inbox now; needs info → review now); contact name and expected timing used
- [x] 6. Language: SWO-aligned consistency checks (name or NPI; no blanket 180-day rule), assignment-qualified cost share, necessity documentation requested, unclear patient understanding → callback
- [x] 45 tests; live Temporal Cloud run with the real model after the schema change
- [x] Review verification: 46 tests including structured delivery-follow-up evidence in the event log; see `FOLLOWUP_REVIEW.md`

## Cut list
- Real telephony / voice AI (behind `VoiceAgent`), inbound calls (simulated by signals)
- DB, auth, UI (Temporal UI shows state)
- Real fax/portal and Medicare claim submission
- Multi-case concurrency tuning, supplier responsiveness history (derivable later from `events.jsonl`)
