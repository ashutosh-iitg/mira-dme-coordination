from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from mira.models import CallDisposition, CallOutcome, CallPurpose, CallRequest, Case, PatientContact
    from mira.policy import MIN_EXTRACTION_CONFIDENCE
    from mira.workflows.common import call_id, escalate, log_event, place_call, sleep_days

MAX_PATIENT_CALLS = 3
PATIENT_RETRY_DAYS = 0.5


def cost_share_point(supplier_name: str, accepts_assignment: bool | None) -> str:
    """Medicare's ~20% applies when the supplier accepts assignment; otherwise they may charge more."""
    base = "Medicare Part B pays 80% of the Medicare-approved amount after the yearly Part B deductible"
    if accepts_assignment:
        return f"{base}; {supplier_name} accepts assignment, so with no supplemental plan she owes about 20%, billed after delivery"
    return (
        f"{base}; she owes about 20% only if {supplier_name} accepts assignment, which we have not confirmed yet. "
        "A supplier that doesn't accept assignment can charge more, so we will confirm before delivery"
    )


def plan_points(supplier_name: str, pcp_name: str, accepts_assignment: bool | None) -> list[str]:
    return [
        f"{pcp_name} signed the written order",
        f"{supplier_name} has agreed to supply the wheelchair",
        cost_share_point(supplier_name, accepts_assignment),
        "Nothing for her to do yet; we'll call when delivery is scheduled",
    ]


def delivery_points(supplier_name: str, eta_days: int | None, accepts_assignment: bool | None) -> list[str]:
    when = f"for about {eta_days} days from now" if eta_days is not None else "(the supplier will call her to set the time)"
    return [
        f"{supplier_name} confirmed delivery is scheduled {when}",
        cost_share_point(supplier_name, accepts_assignment),
        "Someone should be home to receive it",
    ]


class PatientLiaison:
    """Milestone calls to the patient. Problems here never block the case; they create a human callback."""

    def __init__(self) -> None:
        self._calls = 0

    async def update(self, case: Case, milestone: str, points: list[str], seconds_per_day: float) -> PatientContact:
        outcome = await self._reach(case, points, seconds_per_day)
        if outcome is None:
            escalation_id = await escalate(
                case.case_id, f"patient-unreachable-{milestone}", "Patient could not be reached for an update", blocking=False, milestone=milestone
            )
            return PatientContact(milestone=milestone, reached=False, escalation_id=escalation_id)

        result = outcome.patient
        needs_follow_up = (
            result is None
            or outcome.confidence < MIN_EXTRACTION_CONFIDENCE
            or result.understood_cost_share is not True
            or result.understood_next_steps is False
            or bool(result.open_questions)
        )
        escalation_id = None
        if needs_follow_up:
            questions = "; ".join(result.open_questions) if result else "call unclear"
            escalation_id = await escalate(
                case.case_id, f"patient-questions-{milestone}", "Patient has questions a person should answer", blocking=False, questions=questions
            )
        await log_event(case.case_id, "patient_updated", milestone=milestone, follow_up=needs_follow_up)
        return PatientContact(
            milestone=milestone,
            reached=True,
            understood_cost_share=result.understood_cost_share if result else None,
            open_questions=list(result.open_questions) if result else [],
            escalation_id=escalation_id,
        )

    async def _reach(self, case: Case, points: list[str], seconds_per_day: float) -> CallOutcome | None:
        for try_number in range(1, MAX_PATIENT_CALLS + 1):
            self._calls += 1
            outcome = await place_call(
                CallRequest(
                    case_id=case.case_id,
                    call_id=call_id(case.case_id, CallPurpose.PATIENT_UPDATE, case.patient.phone, self._calls),
                    purpose=CallPurpose.PATIENT_UPDATE,
                    party_name=case.patient.name,
                    phone=case.patient.phone,
                    attempt=self._calls,
                    talking_points=points,
                )
            )
            if outcome.disposition is CallDisposition.CONNECTED:
                return outcome
            if try_number < MAX_PATIENT_CALLS:
                await sleep_days(PATIENT_RETRY_DAYS, seconds_per_day)
        return None
