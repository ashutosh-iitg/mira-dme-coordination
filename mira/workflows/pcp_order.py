import asyncio
from dataclasses import dataclass
from datetime import datetime

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from mira.case_activities import CaseActivities
    from mira.models import (
        CallDisposition,
        CallOutcome,
        CallPurpose,
        CallRequest,
        FetchOrderRequest,
        OrderInput,
        OrderOutcomeStatus,
        OrderRequestFax,
        OrderResult,
        PartyContact,
        PcpOrderResult,
        PcpOrderStatus,
        RequirementResolution,
        WrittenOrder,
    )
    from mira.policy import MIN_EXTRACTION_CONFIDENCE, validate_order
    from mira.workflows.common import (
        CASE_ACTIVITY_TIMEOUT,
        DEFAULT_RETRY,
        call_id,
        days_to_timedelta,
        escalate,
        log_event,
        place_call,
        sleep_days,
    )
    from mira.workflows.requirement_gate import RequirementGate


MIN_WAIT_DAYS = 1.0
MAX_WAIT_MULTIPLIER = 2
MAX_REQUIREMENT_ROUNDS = 3


@dataclass(frozen=True)
class NudgeStep:
    order: WrittenOrder | None = None
    escalation_id: str | None = None


@workflow.defn
class PcpOrderWorkflow:
    """Chases the PCP office until a valid signed written order is in hand, or a human is needed."""

    def __init__(self) -> None:
        self._inbound_order: WrittenOrder | None = None
        self._state = "starting"
        self._call_attempts: dict[CallPurpose, int] = {}
        self._fetch_attempts = 0
        self._nudges = 0
        self._contact_name: str | None = None
        self._next_wait_days: float | None = None
        self._requirements = RequirementGate()
        self._requirement_rounds = 0

    @workflow.signal
    def order_received(self, order: WrittenOrder) -> None:
        self._inbound_order = order

    @workflow.signal
    def requirement_resolved(self, result: RequirementResolution) -> None:
        self._requirements.resolve(result)

    @workflow.query
    def state(self) -> str:
        return self._state

    @workflow.run
    async def run(self, inp: OrderInput) -> OrderResult:
        started = workflow.now()
        problems_seen: list[str] = []
        pending_problems: list[str] = []

        self._state = "requesting order"
        request_outcome = await self._call_until_connected(inp, CallPurpose.PCP_ORDER_REQUEST, self._request_points(inp))
        if request_outcome is None:
            await self._fax_request(inp, "office unreachable by phone")
            step = NudgeStep()
        else:
            step = await self._act_on_response(inp, request_outcome, "request")
        if step.escalation_id:
            return self._result(OrderOutcomeStatus.ESCALATED, None, problems_seen, step.escalation_id)
        order = step.order

        while True:
            if order is None:
                order = await self._wait_for_order(inp)
            if order is None:
                if self._over_budget(inp, started):
                    return await self._stalled(inp, problems_seen, self._elapsed_days(inp, started))
                step = await self._nudge(inp, pending_problems)
                if step.escalation_id:
                    return self._result(OrderOutcomeStatus.ESCALATED, None, problems_seen, step.escalation_id)
                order = step.order
                continue

            problems = validate_order(order, inp.equipment.hcpcs_code, inp.patient_name)
            if not problems:
                self._state = "valid order on file"
                await log_event(inp.case_id, "order_validated", code=order.hcpcs_code, prescriber=order.prescriber_name)
                return self._result(OrderOutcomeStatus.VALID, order, problems_seen)

            self._state = "order invalid; requesting correction"
            await log_event(inp.case_id, "order_rejected", problems="; ".join(problems))
            problems_seen.extend(problems)
            pending_problems = problems
            if self._over_budget(inp, started):
                return await self._stalled(inp, problems_seen, self._elapsed_days(inp, started))
            step = await self._nudge(inp, problems)
            if step.escalation_id:
                return self._result(OrderOutcomeStatus.ESCALATED, None, problems_seen, step.escalation_id)
            order = step.order

    def _elapsed_days(self, inp: OrderInput, started: datetime) -> float:
        return (workflow.now() - started).total_seconds() / inp.seconds_per_day

    def _over_budget(self, inp: OrderInput, started: datetime) -> bool:
        return self._nudges >= inp.max_nudges or self._elapsed_days(inp, started) >= inp.sla_days

    async def _wait_for_order(self, inp: OrderInput) -> WrittenOrder | None:
        wait_days = self._next_wait_days or inp.nudge_interval_days
        self._next_wait_days = None
        self._state = f"waiting up to {wait_days:g} days for signed order"
        try:
            await workflow.wait_condition(
                lambda: self._inbound_order is not None,
                timeout=days_to_timedelta(wait_days, inp.seconds_per_day),
            )
        except asyncio.TimeoutError:
            return None
        order, self._inbound_order = self._inbound_order, None
        return order

    async def _nudge(self, inp: OrderInput, problems: list[str]) -> NudgeStep:
        self._nudges += 1
        self._state = f"nudging office (#{self._nudges})"
        outcome = await self._call_until_connected(inp, CallPurpose.PCP_ORDER_NUDGE, self._nudge_points(inp, problems))
        return await self._act_on_response(inp, outcome, f"nudge #{self._nudges}")

    async def _act_on_response(self, inp: OrderInput, outcome: CallOutcome | None, source: str) -> NudgeStep:
        """One handler for every office response, so the first call is acted on exactly like a nudge."""
        status = self._order_status(outcome)
        pcp = outcome.pcp if outcome else None
        if pcp and pcp.contact_name:
            self._contact_name = pcp.contact_name
        await log_event(inp.case_id, "pcp_response", source=source, status=status, contact=self._contact_name or "")

        if status is PcpOrderStatus.NOT_RECEIVED:
            await self._fax_request(inp, "office reports request never received")
        elif status is PcpOrderStatus.NEEDS_INFO:
            return await self._handle_needs_info(inp, outcome, pcp)
        elif status is PcpOrderStatus.SIGNED_AND_SENT:
            return NudgeStep(order=await self._check_fax_inbox(inp))
        elif status is PcpOrderStatus.IN_PROGRESS and pcp and pcp.expected_in_days:
            self._next_wait_days = min(max(float(pcp.expected_in_days), MIN_WAIT_DAYS), inp.nudge_interval_days * MAX_WAIT_MULTIPLIER)
        return NudgeStep()

    async def _handle_needs_info(self, inp: OrderInput, outcome: CallOutcome, pcp: PcpOrderResult) -> NudgeStep:
        """Hand an administrative request to the case coordinator; anything it can't clear goes to a human."""
        requirement = pcp.information_request
        detail = ""
        if inp.coordinator_enabled and requirement and self._requirement_rounds < MAX_REQUIREMENT_ROUNDS:
            self._requirement_rounds += 1
            self._state = f"coordinator resolving PCP requirement: {requirement.description}"
            requester = PartyContact(role="pcp", name=f"{inp.pcp.practice} ({inp.pcp.name})", phone=inp.pcp.phone)
            result = await self._requirements.wait(inp.case_id, requester, outcome.call_id, requirement, inp.seconds_per_day)
            if result.cleared:
                return await self._ask_to_sign(inp, result)
            detail = result.reason
        escalation_id = await escalate(
            inp.case_id, "pcp-needs-info", "PCP office needs information before signing", notes=pcp.notes, reason_detail=detail
        )
        return NudgeStep(escalation_id=escalation_id)

    async def _ask_to_sign(self, inp: OrderInput, result: RequirementResolution) -> NudgeStep:
        self._nudges += 1
        self._state = "PCP accepted the requested information; asking the office to sign"
        points = [
            *self._nudge_points(inp, []),
            f"You accepted {result.accepted_reference} as the information you needed; please sign and send the written order",
        ]
        outcome = await self._call_until_connected(inp, CallPurpose.PCP_ORDER_NUDGE, points)
        return await self._act_on_response(inp, outcome, f"after requirement {result.task_id}")

    async def _call_until_connected(self, inp: OrderInput, purpose: CallPurpose, points: list[str]) -> CallOutcome | None:
        for try_number in range(1, inp.max_call_attempts + 1):
            attempt = self._call_attempts.get(purpose, 0) + 1
            self._call_attempts[purpose] = attempt
            outcome = await place_call(
                CallRequest(
                    case_id=inp.case_id,
                    call_id=call_id(inp.case_id, purpose, inp.pcp.phone, attempt),
                    purpose=purpose,
                    party_name=f"{inp.pcp.practice} ({inp.pcp.name})",
                    phone=inp.pcp.phone,
                    attempt=attempt,
                    talking_points=points,
                )
            )
            if outcome.disposition is CallDisposition.CONNECTED:
                return outcome
            if try_number < inp.max_call_attempts:
                await sleep_days(inp.no_answer_retry_days, inp.seconds_per_day)
        return None

    async def _check_fax_inbox(self, inp: OrderInput) -> WrittenOrder | None:
        self._fetch_attempts += 1
        order = await workflow.execute_activity_method(
            CaseActivities.fetch_written_order,
            FetchOrderRequest(case_id=inp.case_id, fetch_attempt=self._fetch_attempts),
            start_to_close_timeout=CASE_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY,
        )
        if order is None:
            await log_event(inp.case_id, "order_not_in_inbox", detail="office said sent; nothing arrived yet")
        return order

    async def _fax_request(self, inp: OrderInput, reason: str) -> None:
        await workflow.execute_activity_method(
            CaseActivities.send_order_request,
            OrderRequestFax(case_id=inp.case_id, pcp=inp.pcp, patient_name=inp.patient_name, equipment=inp.equipment, reason=reason),
            start_to_close_timeout=CASE_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY,
        )

    async def _stalled(self, inp: OrderInput, problems_seen: list[str], elapsed_days: float) -> OrderResult:
        self._state = "stalled; escalated"
        escalation_id = await escalate(
            inp.case_id,
            "order-stalled",
            "Written order not obtained within SLA",
            nudges=self._nudges,
            elapsed_days=round(elapsed_days, 1),
            problems="; ".join(problems_seen) or "none",
        )
        return self._result(OrderOutcomeStatus.ESCALATED, None, problems_seen, escalation_id)

    def _result(
        self, status: OrderOutcomeStatus, order: WrittenOrder | None, problems_seen: list[str], escalation_id: str | None = None
    ) -> OrderResult:
        return OrderResult(status=status, order=order, nudges=self._nudges, problems_seen=problems_seen, escalation_id=escalation_id)

    @staticmethod
    def _order_status(outcome: CallOutcome | None) -> PcpOrderStatus:
        if outcome is None or outcome.pcp is None or outcome.confidence < MIN_EXTRACTION_CONFIDENCE:
            return PcpOrderStatus.UNCLEAR
        return outcome.pcp.order_status

    @staticmethod
    def _request_points(inp: OrderInput) -> list[str]:
        return [
            f"Patient {inp.patient_name} was seen by {inp.pcp.name}; verbal order for {inp.equipment.description} is in the chart",
            f"Need a signed standard written order for HCPCS {inp.equipment.hcpcs_code}, quantity 1, with prescriber name/NPI",
            "Confirm the visit note documents her mobility limitation, why a cane or walker is not enough, "
            "and that she can use a manual wheelchair at home (self-propelled or with a caregiver)",
            "Ask who to follow up with and when it will be signed",
        ]

    def _nudge_points(self, inp: OrderInput, problems: list[str]) -> list[str]:
        points = [f"Following up on the written order for {inp.patient_name} ({inp.equipment.hcpcs_code})"]
        if self._contact_name:
            points.append(f"Ask for {self._contact_name}, who handled this before")
        if problems:
            points.append("The order received has problems that would get the claim denied: " + "; ".join(problems))
            points.append("Ask them to correct and re-send it")
        return points
