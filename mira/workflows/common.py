from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from mira.activities import CallActivities
    from mira.case_activities import CaseActivities
    from mira.config import VOICE_CALL_TASK_QUEUE
    from mira.models import CallOutcome, CallPurpose, CallRequest, CaseEvent, Escalation

CALL_TIMEOUT = timedelta(minutes=3)
CASE_ACTIVITY_TIMEOUT = timedelta(seconds=30)
DEFAULT_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=4)


def call_id(case_id: str, purpose: CallPurpose, phone: str, attempt: int) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return f"{case_id}:{purpose}:{digits}:{attempt}"


async def place_call(request: CallRequest) -> CallOutcome:
    return await workflow.execute_activity_method(
        CallActivities.place_call,
        request,
        task_queue=VOICE_CALL_TASK_QUEUE,
        start_to_close_timeout=CALL_TIMEOUT,
        retry_policy=DEFAULT_RETRY,
    )


async def sleep_days(days: float, seconds_per_day: float) -> None:
    await workflow.sleep(timedelta(seconds=days * seconds_per_day))


def days_to_timedelta(days: float, seconds_per_day: float) -> timedelta:
    return timedelta(seconds=days * seconds_per_day)


async def log_event(case_id: str, kind: str, **details: object) -> None:
    await workflow.execute_activity_method(
        CaseActivities.record_event,
        CaseEvent(case_id=case_id, kind=kind, details={k: str(v) for k, v in details.items()}),
        start_to_close_timeout=CASE_ACTIVITY_TIMEOUT,
        retry_policy=DEFAULT_RETRY,
    )


async def escalate(case_id: str, key: str, reason: str, blocking: bool = True, **context: object) -> str:
    """Blocking escalations pause the case until a human decides; non-blocking ones are FYI callbacks."""
    escalation = Escalation(
        escalation_id=f"{workflow.info().workflow_id}:{key}",
        case_id=case_id,
        reason=reason,
        context={"blocking": str(blocking).lower(), **{k: str(v) for k, v in context.items()}},
    )
    return await workflow.execute_activity_method(
        CaseActivities.escalate,
        escalation,
        start_to_close_timeout=CASE_ACTIVITY_TIMEOUT,
        retry_policy=DEFAULT_RETRY,
    )
