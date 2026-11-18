# Assignment implementation review

Reviewed 18 November 2026 against `assignment.md`, `plan.md`, the current source, README, writeup, and tests. This is an assessment and proposed follow-up; application code was not changed for this review.

## Assessment

This is a strong take-home prototype with a coherent end-to-end coordination loop. The best decisions are durable orchestration, parallel supplier/order work, explicit fallback behavior, and a narrow, real LLM extraction step separated from deterministic policy. It demonstrates substantially more than a pipeline diagram or chatbot.

All five implementation phases in the plan have corresponding code. However, checked boxes overstate the completeness of human callbacks, technical failure recovery, and delivery confirmation semantics. The default scenario is convincing; uncertain and exceptional inputs need more scrutiny before the system can reliably work unattended.

My recommendation is to spend the extra time tightening those boundaries and demonstrating them. A new framework, database, UI, or telephony integration would add less value in this time window.

## Verification and limits

- Ran `.venv/bin/python -m pytest -q`: **28 passed**, including three Temporal time-skipping workflow tests. The Temporal server required execution outside the filesystem sandbox. Initial sandbox startup errors were environmental, not test failures.
- Checked the CLI module import and inspected the updated shared human-review client.
- Executed direct policy probes: unknown service area qualified; an ETA with no explicit delivery commitment counted as scheduled; a commitment without timing also counted as scheduled; negative ETA values passed model validation.
- Did not launch another Temporal Cloud case, place calls, or make paid LLM requests. Existing claims of live model testing and worker restart recovery are documented in the repository, not independently repeated here.
- Source files changed during the review. The initial human-signal mismatch was fixed by those concurrent changes, and the final test run used the updated review workflow and CLI. It is not an outstanding finding.

## Requirement and plan coverage

| Assignment expectation | Current implementation | Assessment |
|---|---|---|
| Work sparse supplier directory by phone | CSV loader, ZIP ranking, sequential supplier screening, bounded callbacks | Implemented; geographic cutoff and unknown coverage handling need adjustment |
| Chase the PCP order | Durable nudges, lost-request re-fax, typed order validation, correction call, SLA escalation | Implemented; initial call status is ignored |
| Match order and supplier | Code check, mock order transfer, follow-up calls, backup fallback | Implemented; tentative availability can be mistaken for a scheduled delivery |
| Keep Eleanor informed | Plan/delivery calls, cost explanation, bounded no-answer retries | Implemented after sourcing/order finish; no updates during long stalls |
| Ask humans only when needed | Blocking child review, retry/close, interactive demo and CLI | Blocking path tested; non-blocking callbacks are absent from the current inbox |
| Real LLM inside running system | OpenRouter transcript extraction with Pydantic validation and schema repair | Real runtime integration; workflow tests use fakes and do not measure extraction accuracy |
| Explicit mocks and cuts | README and writeup distinguish phone/fax mocks and scheduled-versus-delivered endpoint | Good; strengthen billing/coverage qualifications and refresh CLI documentation |
| Runnable deliverable and short writeup | uv project, lockfile, README, scenario files, writeup | Present; reviewer needs their own Temporal Cloud configuration and model key |

Ending at delivery scheduled is a reasonable, explicitly disclosed cut. Real delivery confirmation is not required to make this submission credible. The system must nevertheless establish that scheduling actually happened before reporting that result.

## Findings, in recommended priority order

### 1. Delivery can be reported as scheduled without scheduling evidence

`mira/policy.py:63` reuses the screening result for fulfillment and accepts either `committed_to_deliver=True` **or any non-null ETA**. A statement such as “usually four days after eligibility clears” can therefore complete the case despite no actual schedule. `delivery_points` then tells Eleanor delivery is scheduled.

Use a separate follow-up extraction with explicit order receipt and scheduling status, distinguishing tentative ETA from confirmed scheduling. Require affirmative scheduling evidence; timing can remain unknown if scheduling itself was explicitly confirmed. Validate ETA as nonnegative. Add tests for conditional ETAs, acceptance without scheduling, confirmed scheduling, and contradictory responses.

### 2. Patient callbacks disappear from the human inbox

`PatientLiaison.update` creates non-blocking escalations through `CaseActivities.escalate`, which writes JSON. `mira/reviews.py:16` lists only running `HumanReviewWorkflow` executions. Those executions are created by the blocking `_await_human` path, so patient callbacks never appear in `mira.human list` and have no supported completion action. The default scenario itself produces one of these callbacks.

For this prototype, merge open JSON callback records into the inbox and provide a callback completion action. Keep retry/close for blocking reviews. Verify that completing a callback after the case finishes does not reopen or retry the case.

### 3. Technical failures bypass business escalation

The call activity retries exceptions, but exhausted retries surface through child workflows. The parent catches only `CaseClosed` (`mira/workflows/case.py:72`). Persistent LLM errors, malformed outputs after repair, or voice-provider errors can fail the workflow without an actionable human review.

Catch expected activity/child failures at an appropriate boundary and create a technical review containing the failed step and error category. Permit retry of that step after recovery. Avoid catching programming defects indiscriminately or converting provider errors into false “no answer” business outcomes. Test a forced provider failure followed by recovery.

### 4. Supplier coverage is inferred too generously and too narrowly

`assess_supplier_call` rejects explicit `serves_patient_area=False`, but omits that field from its unknown-value checks, allowing `None` to qualify. Separately, `_drop_out_of_range` rejects suppliers beyond an assumed 15-mile ZIP-centroid radius without calling them. The directory does not establish a supplier's delivery territory.

Require affirmative service-area confirmation. Use distance to order calls; broaden the search before “no supplier” escalation rather than treating distance as proof of inability to serve. Also fix the ETA sort: `eta or 999` incorrectly ranks same-day delivery (`0`) as unknown.

### 5. Initial PCP response does not drive the next action

`PcpOrderWorkflow.run:66` only checks whether the initial call connected. If the office immediately says “signed and sent,” the workflow waits two simulated days instead of checking the inbox. If it says “we need a mobility assessment,” it similarly delays the actionable escalation. Contact name and expected completion time are extracted but unused.

Route the initial result through the same status handler as nudges. Add tests for initial signed/sent and initial needs-info. Keep deadline scheduling simple within this time box.

### 6. Coverage and billing language overstates validation

The deterministic validator is useful for detecting case-specific mismatches; passing it does not establish medical necessity or guarantee payment. CMS's general SWO requirements allow practitioner name **or** NPI and alternative item descriptions. The current validator additionally requires NPI, specific description words, and a universal 180-day visit gap. Document deliberate prototype checks separately from verified coverage rules. The six-month face-to-face rule applies to specified items; do not assume a universal 180-day equivalent. [CMS documentation requirements](https://www.cms.gov/medicare-coverage-database/view/article.aspx?articleid=55426).

Patient messaging should qualify the approximate 20% share on the supplier accepting assignment, which is not currently collected in screening. [Medicare wheelchair coverage and costs](https://www.medicare.gov/coverage/wheelchairs-scooters).

Medical necessity also depends on evidence about mobility limitations, cane/walker insufficiency, home suitability, and ability to propel or caregiver assistance. Intake does not establish all of these. Request missing documentation or explicitly leave its review with the supplier; avoid portraying order-field validation as complete eligibility verification. [CMS manual wheelchair guidance](https://www.cms.gov/training-education/medicare-learning-networkr-mln/compliance/medicare-provider-compliance-tips/manual-wheelchairs).

## Quality of architecture, tests, and presentation

The code is small enough to understand, organized around actual case responsibilities, and uses typed boundaries and dependency injection effectively. Temporal is well matched to the waiting/retry problem. The writeup answers sequencing, architecture, cuts, and the one-day/two-week questions well. Its weakest claims are “billable” order validation and the broad statement that low-confidence results always cause another call: patient uncertainty can escalate immediately, and null understanding can pass without escalation.

The tests prove the scripted journey and two human-close paths. They do not yet establish human-retry recovery, initial PCP status handling, uncertain fulfillment, non-blocking callback lifecycle, provider failure recovery, inbound order signals, or model extraction quality. The fake completer uses known transcript phrases; this tests orchestration, not the LLM's comprehension. A small reviewed extraction dataset is a better next quality investment than expanding scenario volume without assertions about individual fields.

Other follow-ups: patient understanding of next steps is extracted but ignored; `understood_cost_share=None` is not treated as unresolved. Calls and logs share one activity, so a later extraction/logging failure repeats the call activity; stable IDs help mocks but do not themselves guarantee real-provider idempotency. JSON escalation writes are synchronous and not atomic. These are worth acknowledging, but a production storage redesign is outside this follow-up.

## Concrete additional-time plan

Estimates include focused regression tests and assume the current architecture is retained.

| Time | Work | Acceptance evidence |
|---|---|---|
| 0–25 min | Separate fulfillment facts from screening; require explicit scheduled status; require service-area confirmation; reject negative ETA and fix zero-ETA sorting | Conditional ETA stays pending; unknown area retries; confirmed schedule completes; same-day ETA sorts correctly |
| 25–45 min | Restore callback visibility and add completion action | Default patient questions appear in inbox and can be completed after the case ends |
| 45–60 min | Run the suite, refresh README/plan/writeup, qualify cost and coverage claims | Green tests; documented demo matches actual CLI; clear scheduled-delivery boundary |
| 60–95 min | Add technical-failure escalation and retry of the affected step | Injected provider error becomes a visible review; retry after recovery continues the case |
| 95–110 min | Handle initial PCP status immediately | Signed/sent checks inbox immediately; needs-info immediately creates review |
| 110–120 min | Run final checks and rehearse the default and escalation demos | Show screening rejection, order correction, supplier fallback, visible callback, and human recovery |

With only one hour, stop after the first three rows. With two hours, finish failure recovery before extending scope; if it overruns, defer the PCP refactor. The time estimates are planning estimates, not guarantees.

After this window, prioritize a small field-level extraction evaluation set, then one real supplier-call integration. Keep the one-day plan focused on proving extraction and action reliability; an advocate UI can follow a complete inbox lifecycle. Within two weeks, add inbound callbacks, fax ingestion, delivery confirmation, and business-hours scheduling before optimizing many-case throughput.

For the live defense, emphasize: “The system performs the waiting, chasing, correction, and fallback itself; the model extracts observations, and explicit rules decide actions. The prototype ends at confirmed scheduling, and each remaining human task stays visible.”
