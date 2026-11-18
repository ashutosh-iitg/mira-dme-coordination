# Review of the six follow-ups

Reviewed 18 November 2026. This supersedes the outstanding-finding status in the original `REVIEW.md`; that document remains the historical assessment.

All six follow-ups have corresponding implementation and are acceptable for the current prototype scope.

| Original finding | Reviewed implementation | Verification |
|---|---|---|
| Delivery scheduling evidence | Dedicated follow-up schema; tentative and unclear responses retry; explicit scheduling completes; contradictory missing-order response does not complete | Policy cases for tentative, accepted but not scheduled, scheduled without a date, contradiction, decline, and unclear; negative ETA rejected |
| Invisible callbacks | Inbox combines live Temporal reviews and stored callbacks; `done` completes callbacks without signaling the case | Default workflow completes a callback after case completion; duplicate completion and completion of a blocking review are rejected |
| Technical failures | Child failures become technical reviews; retry starts a new child for that track; patient-call failures become callbacks | Injected provider outage followed by recovery completes the case and resolves both technical reviews |
| Supplier service coverage | Affirmative delivery-area confirmation required; distance orders calls rather than excluding suppliers; same-day ETA sorts correctly | Unknown/false coverage and same-day/unknown ETA tests |
| Initial PCP status ignored | Initial request and nudges share a response handler; signed/sent checks inbox immediately; needs-info escalates immediately | Both first-response paths tested with zero nudges |
| Coverage and cost claims | Order checks documented as consistency checks; practitioner name or NPI accepted; blanket 180-day rule removed; cost messaging qualified by assignment; unclear cost understanding creates callbacks | Updated order-policy tests, prompts, scenarios, README, and writeup |

One regression found during review was corrected: `CallActivities.place_call` did not include the newly introduced `outcome.followup` in its event payload. Follow-up evidence is now logged, and all extracted results are serialized as structured JSON rather than Pydantic string representations. A regression test verifies the actual scheduling evidence survives the activity boundary.

Validation: **46 tests passed**, including six Temporal workflow tests; `git diff --check` passed. Tests use fake model responses; no additional live model/Cloud run was performed during this review. The plan's live-run statement belongs to the implementation author's verification.

Remaining limits are relevant to the next architecture, rather than blockers for committing these changes:

- Technical `retry` restarts a whole child track. It does not resume at the failed external action; completed calls or mock handoffs can repeat after a partial track failure. Real integrations need persisted action receipts and idempotency/reconciliation.
- Atomic JSON replacement prevents partially written records, but does not serialize multiple writers. The shared temporary filename and read/modify/write cycle are unsuitable for multiple workers or a CLI modifying the file concurrently. Use a transactional store for the expanded agent system.
- Assignment is collected and messaging is conditional, but unknown/refused assignment does not yet create an autonomous quote/clarification task. Current wording promises a later confirmation without implementing that follow-up. This is an appropriate first coordination-agent use case.
- A patient with unknown next-step understanding and confirmed cost understanding can still avoid a callback; only explicit false next-step understanding is checked. Avoid claiming that every unknown field blocks progress.
- Clinical documentation is requested but not verified for clinical sufficiency. No coverage or payment guarantee follows from passing order consistency checks.

The next design should turn unresolved administrative requests into persisted, evidence-backed tasks that an agent can work, rather than treating every request as an immediate human escalation.
