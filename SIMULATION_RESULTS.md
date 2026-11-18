# End-to-end Temporal simulation results — 18 November 2026

All five scenarios were run sequentially through a real local Temporal time-skipping server. The coordinator and DME call extraction used live Groq `openai/gpt-oss-120b`; voice, fax, appointment booking, supplier availability, external confirmations and human responses were mocked.

**5/5 scenario checks passed, including all 12 expected coordinator decisions.** Four cases reached `delivery_scheduled`; the clinical case requested human review and did not enter DME fulfillment. This is simulated scheduling, not physical delivery or claim payment.

| Scenario | Coordinator and case outcome | Mock calls | Exported histories |
|---|---|---|---|
| delayed_pt_reply | `delivery_scheduled` | 11 | 6 |
| expired_slot | `delivery_scheduled` | 12 | 6 |
| unaffordable_quote | `delivery_scheduled` | 13 | 6 |
| superseded_quote | `delivery_scheduled` | 11 | 6 |
| clinical_boundary | `human_review_required` | 1 | 3 |

## Timing and request budget

- Actual execution: 13:34:25 IST–13:48:24 IST (13m 58s).
- Model requests: **37**. Observed rate-limit responses: **0**.
- Reported model input/output tokens: **55,747 / 13,101**.
- Additional Temporal activity retry attempts: **1**.
- The coordinator proposal tool now ends the agent turn directly, avoiding a redundant model response after the proposal.
- The local server accelerates coordination delays. The existing DME child retains its documented 60-seconds-per-service-day demo scale.

## What the traces demonstrate

The delayed-reply case made a mock patient call that was not answered, waited six simulated hours on a Temporal timer, received a scripted callback, booked the accepted appointment, waited for PCP report acceptance, and only then unblocked the DME case. The DME child validated the signed order, qualified suppliers through mock calls with real extraction, handed off the order, confirmed a scheduled delivery, and updated the patient.

The expired-offer case requested and received a replacement offer before booking. The cost cases retained patient acceptance against the correct quote. The clinical case explicitly proposed `request_human_review`; a real `HumanReviewWorkflow` then received a scripted human response after a one-hour simulated delay. No DME case was started for that clinical request.

One coordinator model turn finished without proposing an action. Temporal retried it and recovered without advancing state or repeating an already committed coordination call. The coordinator history retains the successful start at attempt 2; intermediate activity-failure details may be compacted by Temporal.

## Inspect each case

Open `runtime/e2e/temporal-groq-five-cases-20261118/index.html` for the filterable run index. Each scenario directory contains:

- `trace.html` and `trace.md`: separate readable timelines with IST display times.
- `trace.jsonl`: raw actual UTC and simulated Temporal timestamps, calls, transcripts, decisions and transitions.
- `histories/`: independent parent/coordinator/DME or human-review histories, including original UTC event times.
- `state/` and `result.json`: JSON lifecycle state and final outcomes.

The run contains **27 exported Temporal histories**. Runtime artifacts and credentials remain ignored by Git. See `SIMULATION.md` for the exact composition and reproduction command.

## Interpretation and limits

The earlier direct-activity evaluation failed the clinical-routing scenario; this integrated run passed it without adding a clinical prompt fix. A successful second run is not proof that the earlier routing weakness is resolved. The regression suite explicitly tests a misrouted clinical request and verifies that the simulation guard prevents DME fulfillment and hands it to human review.

The mock-provider supplies typed consent/report/quote evidence alongside callback transcripts. Those callback facts are not claimed to have been inferred by the live extractor. DME screening, PCP status, delivery confirmation and patient-update call extraction do use the live selected model. External outcomes are scripted and this is one pass per case, not a reliability estimate.

Automated verification: **72 tests passed**, including all five complete Temporal/mock-voice/DME paths and the clinical-misroute guard.
