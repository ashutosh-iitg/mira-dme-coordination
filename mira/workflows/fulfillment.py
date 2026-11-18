from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from mira.case_activities import CaseActivities
    from mira.models import (
        CallPurpose,
        CallRequest,
        FulfillmentAttempt,
        FulfillmentInput,
        FulfillmentResult,
        HandoffResult,
        SupplierHandoff,
        SupplierVerdict,
    )
    from mira.policy import FollowupDecision, assess_followup, claim_code_problems
    from mira.workflows.common import (
        CASE_ACTIVITY_TIMEOUT,
        DEFAULT_RETRY,
        call_id,
        escalate,
        log_event,
        place_call,
        sleep_days,
    )

DECISION_TO_RESULT = {FollowupDecision.SCHEDULED: HandoffResult.SCHEDULED, FollowupDecision.DECLINED: HandoffResult.DECLINED}


@workflow.defn
class FulfillmentWorkflow:
    """Hands the signed order to suppliers in preference order until one confirms delivery."""

    def __init__(self) -> None:
        self._attempts: list[FulfillmentAttempt] = []
        self._state = "starting"

    @workflow.query
    def state(self) -> str:
        return self._state

    @workflow.run
    async def run(self, inp: FulfillmentInput) -> FulfillmentResult:
        problems = claim_code_problems(inp.order, inp.equipment.hcpcs_code)
        if problems:
            escalation_id = await escalate(inp.case_id, "claim-mismatch", "Order code does not match the item", problems="; ".join(problems))
            return FulfillmentResult(scheduled_with=None, delivery_eta_days=None, attempts=[], escalation_id=escalation_id)

        for candidate in inp.candidates:
            attempt = await self._hand_off(inp, candidate)
            self._attempts.append(attempt)
            if attempt.result is HandoffResult.SCHEDULED:
                self._state = f"delivery scheduled with {candidate.supplier.supplier_name}"
                return FulfillmentResult(
                    scheduled_with=candidate, delivery_eta_days=attempt.delivery_eta_days, attempts=list(self._attempts)
                )

        self._state = "no supplier confirmed delivery; escalated"
        escalation_id = await escalate(
            inp.case_id,
            "no-delivery",
            "Every lined-up supplier went silent or declined after receiving the order",
            tried=", ".join(f"{a.supplier_name} ({a.result})" for a in self._attempts),
        )
        return FulfillmentResult(scheduled_with=None, delivery_eta_days=None, attempts=list(self._attempts), escalation_id=escalation_id)

    async def _hand_off(self, inp: FulfillmentInput, candidate: SupplierVerdict) -> FulfillmentAttempt:
        supplier = candidate.supplier
        self._state = f"order sent to {supplier.supplier_name}; awaiting confirmation"
        await workflow.execute_activity_method(
            CaseActivities.send_order_to_supplier,
            SupplierHandoff(
                case_id=inp.case_id, supplier=supplier, patient_name=inp.patient_name, patient_zip=inp.patient_zip, order=inp.order
            ),
            start_to_close_timeout=CASE_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY,
        )
        await sleep_days(inp.followup_after_days, inp.seconds_per_day)

        for call_number in range(1, inp.max_followup_calls + 1):
            outcome = await place_call(
                CallRequest(
                    case_id=inp.case_id,
                    call_id=call_id(inp.case_id, CallPurpose.SUPPLIER_FOLLOWUP, supplier.phone, call_number),
                    purpose=CallPurpose.SUPPLIER_FOLLOWUP,
                    party_name=supplier.supplier_name,
                    phone=supplier.phone,
                    attempt=call_number,
                    talking_points=[
                        f"Following up on the signed {inp.order.hcpcs_code} order sent for {inp.patient_name}",
                        "Ask: did you receive it, is delivery scheduled, and when?",
                    ],
                )
            )
            decision = assess_followup(outcome)
            if decision in DECISION_TO_RESULT:
                eta = outcome.supplier.delivery_eta_days if outcome.supplier else None
                await log_event(inp.case_id, "supplier_followup", supplier=supplier.supplier_name, result=decision, eta_days=eta)
                return FulfillmentAttempt(
                    supplier_name=supplier.supplier_name,
                    phone=supplier.phone,
                    result=DECISION_TO_RESULT[decision],
                    followup_calls=call_number,
                    delivery_eta_days=eta,
                )
            if call_number < inp.max_followup_calls:
                await sleep_days(inp.followup_retry_days, inp.seconds_per_day)

        await log_event(inp.case_id, "supplier_went_silent", supplier=supplier.supplier_name, calls=inp.max_followup_calls)
        return FulfillmentAttempt(
            supplier_name=supplier.supplier_name, phone=supplier.phone, result=HandoffResult.SILENT, followup_calls=inp.max_followup_calls
        )
