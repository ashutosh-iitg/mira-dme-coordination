# Writeup: automating DME back-end coordination

## Sequencing: what I built and how I decided

The job is long-running coordination with three slow, unreliable parties reached by phone. What eats a care
advocate's week isn't any single step; it's waiting, remembering to call back, and noticing when something
quietly stalled. So I built in this order:

1. **The durable case runner.** Every failure mode in the brief ("goes silent", "falls in a hole", "patient goes
   quiet") is a timer that has to fire days later and a decision about what to do when it does. If that layer
   isn't trustworthy, nothing above it matters. It's a Temporal workflow per case, with child workflows for each
   track.
2. **The calling queue.** Calls are the system's only way of acting on the world. A dedicated task queue with a
   concurrency cap (phone lines) runs `place_call`: voice agent → transcript → LLM → typed outcome. The voice
   agent is a placeholder behind an interface; the LLM is real.
3. **The two tracks that start immediately, in parallel.** Supplier sourcing works the directory closest-first,
   rejects on what suppliers say, and calls no-answers back on a timer. The PCP order chase requests, nudges,
   re-faxes when the office "never got it", and validates the order that comes back.
4. **Handoff, patient updates, and the human loop.** The order goes to the primary supplier. If the supplier goes
   silent after saying yes, the case falls back to the backup. The patient is called at milestones. Escalations
   either pause the case until a human decides (`retry`/`close`) or, for patient questions, create a callback
   without blocking. A pause is its own `HumanReviewWorkflow`, so "waiting on a person" is a first-class,
   visible, durable task in Temporal, not a flag in a database.

Each scenario failure in the brief is scripted in `data/scenarios/default.json`, and the demo hits all five.

## Technology and architecture

- **Temporal Cloud** for orchestration. Durable timers make "call back tomorrow" and "nudge in two days"
  first-class, and they survive crashes. I killed the worker mid-case and the case resumed from history. Signals
  model inbound events (a human decision, an order arriving). Queries give live case status. Temporal's UI
  doubles as the ops console. No local infra: credentials come from the Temporal CLI profile, not the repo.
- **Python, Pydantic, the `openai` SDK pointed at OpenRouter.** No agent framework: the LLM has one narrow job,
  and a framework would hide where it sits. Output is schema-validated, with one repair re-prompt, and works
  with models that don't support `response_format`.
- **Wiring:** `CaseWorkflow` runs `SupplierSourcingWorkflow` ∥ `PcpOrderWorkflow`, then `FulfillmentWorkflow`,
  with `PatientLiaison` calls in between. Two task queues: `dme-cases` (workflows, case activities) and
  `voice-calls` (calls only, capped). JSON/CSV files stand in for the database: the event log, escalations, and
  scenarios.

**Where AI sits:** turning a messy phone conversation into structured facts ("two, three days tops" → 3 days;
"we're enrolled but stopped taking new Medicare patients" → not accepting), with a confidence score. This is
the part that's genuinely hard to do with rules.

**Where I kept AI out, on purpose:** whether a supplier qualifies (including an affirmative "we deliver to her
area"), whether a delivery is actually scheduled (explicit booking, not typical timing), whether the written order
is consistent with the case (HCPCS match plus standard-written-order elements), when to retry, fall back or
escalate, and whether a patient call needs a human. These are in `policy.py` and the workflows as plain, tested
code. A wrong call here costs the patient the full bill, so decisions must be auditable and deterministic.
Unknown or low-confidence facts never advance a case: for suppliers and the PCP they mean "call again"; for the
patient they create a human callback. One live run showed why: the model read a range ETA as unknown, and the
ranking rule (known ETA beats unknown) handled it safely. I then tightened the prompt.

The order checks are consistency checks, not a coverage decision. Medical necessity documentation is requested
from the PCP and left with the PCP and supplier, and the ~20% patient share is quoted only as conditional on the
supplier accepting assignment.

**Failures are cases too.** A business problem (no supplier, stalled order) and a technical one (LLM provider
down, bad config) both become a visible human review with retry/close. Technical failures are never turned into
a fake "no answer".

## Cut list

- **Real telephony and voice AI.** Mocked behind `VoiceAgent`. Talking points are already passed per call, ready
  for a real agent's script.
- **Inbound calls.** Suppliers or offices calling back are modeled only as signals.
- **Eligibility, enrollment verification, medical-necessity review, claim submission, delivery confirmation.**
  The case ends at a supplier-confirmed scheduled delivery. The directory is treated as the enrollment source, as
  the brief says.
- **Extraction quality measurement.** Workflow tests use a fake LLM, so they prove orchestration, not that the
  model reads calls correctly. A field-level eval set is the first thing I'd add.
- **Real fax and portal.** Mocked. The signed order arrives through a scripted inbox or a signal.
- **DB, auth, UI.** JSON files and the Temporal UI.
- **Patient address and phone** aren't in the intake. They're placeholders, flagged in `case.json`.
- **Supplier memory across cases** (who answers, who flakes) and multi-case scheduling.

## What's next

**With one more day:** a real voice agent on one call type (supplier screening, the highest volume), with
recordings and transcripts stored per call. Then an eval set of real transcripts to measure extraction accuracy
per field, since that is the AI's only job and the system's only blind spot. Plus a small advocate inbox UI over
`escalations.json`, so humans act from a queue instead of a CLI.

**With two weeks:** inbound call handling (callbacks routed to the right case via a signal), real fax/e-fax in
and out with OCR of signed orders into the same validation path, eligibility and enrollment checks before the
first supplier call, and a supplier reliability score learned from the event log to reorder the call list. Then
run many cases concurrently and tune phone-line capacity and business-hours calling windows.

**Why that order:** first make the one AI-dependent step measurably reliable on real audio, because every
decision downstream trusts it. Then close the loops where cases still lean on humans today (inbound calls,
paperwork). Only then optimize throughput, because throughput is worthless if cases stall.
