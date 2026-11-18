"""Client-side access to human reviews: shared by the advocate CLI and the interactive demo."""

from enum import StrEnum

from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from mira.escalation_store import OPEN, EscalationStore, is_blocking
from mira.models import Escalation, Frozen, HumanAction, HumanDecision
from mira.workflows.human_review import HumanReviewWorkflow, review_workflow_id

PENDING_REVIEWS_QUERY = 'WorkflowType="HumanReviewWorkflow" AND ExecutionStatus="Running"'


class ReviewNotFound(LookupError):
    pass


class InboxKind(StrEnum):
    REVIEW = "review"
    CALLBACK = "callback"


class InboxItem(Frozen):
    kind: InboxKind
    escalation: Escalation


async def list_pending_reviews(client: Client) -> list[Escalation]:
    pending: list[Escalation] = []
    async for execution in client.list_workflows(PENDING_REVIEWS_QUERY):
        request = await client.get_workflow_handle(execution.id).query(HumanReviewWorkflow.request)
        if request is not None:
            pending.append(request)
    return pending


async def list_inbox(client: Client, store: EscalationStore) -> list[InboxItem]:
    """Blocking reviews come from Temporal (the source of truth for what a case waits on); callbacks from the store."""
    reviews = [InboxItem(kind=InboxKind.REVIEW, escalation=e) for e in await list_pending_reviews(client)]
    callbacks = [InboxItem(kind=InboxKind.CALLBACK, escalation=e) for e in store.open_callbacks()]
    return [*reviews, *callbacks]


def complete_callback(store: EscalationStore, escalation_id: str, note: str) -> None:
    """Callbacks never feed back into a case, so completing one after the case ends cannot reopen it."""
    record = store.get(escalation_id)
    if record is None:
        raise ReviewNotFound(f"No escalation {escalation_id}")
    if is_blocking(record):
        raise ReviewNotFound(f"{escalation_id} is a blocking review; use `decide` with retry or close")
    if record.get("status") != OPEN:
        raise ReviewNotFound(f"Callback {escalation_id} is already {record.get('status')}")
    store.update(escalation_id, {"status": "completed", "action": "callback_completed", "note": note})


async def send_decision(client: Client, escalation_id: str, action: HumanAction, note: str) -> str:
    review_id = review_workflow_id(escalation_id)
    try:
        await client.get_workflow_handle(review_id).signal(
            HumanReviewWorkflow.decide, HumanDecision(escalation_id=escalation_id, action=action, note=note)
        )
    except RPCError as exc:
        if exc.status is RPCStatusCode.NOT_FOUND:
            raise ReviewNotFound(
                f"No open human review for {escalation_id} (already decided, a callback — use `done`, or a wrong id)"
            ) from exc
        raise
    return review_id
