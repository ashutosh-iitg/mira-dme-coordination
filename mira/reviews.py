"""Client-side access to human reviews: shared by the advocate CLI and the interactive demo."""

from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from mira.models import Escalation, HumanAction, HumanDecision
from mira.workflows.human_review import HumanReviewWorkflow, review_workflow_id

PENDING_REVIEWS_QUERY = 'WorkflowType="HumanReviewWorkflow" AND ExecutionStatus="Running"'


class ReviewNotFound(LookupError):
    pass


async def list_pending_reviews(client: Client) -> list[Escalation]:
    pending: list[Escalation] = []
    async for execution in client.list_workflows(PENDING_REVIEWS_QUERY):
        request = await client.get_workflow_handle(execution.id).query(HumanReviewWorkflow.request)
        if request is not None:
            pending.append(request)
    return pending


async def send_decision(client: Client, escalation_id: str, action: HumanAction, note: str) -> str:
    review_id = review_workflow_id(escalation_id)
    try:
        await client.get_workflow_handle(review_id).signal(
            HumanReviewWorkflow.decide, HumanDecision(escalation_id=escalation_id, action=action, note=note)
        )
    except RPCError as exc:
        if exc.status is RPCStatusCode.NOT_FOUND:
            raise ReviewNotFound(f"No open human review for {escalation_id} (already decided, or wrong id)") from exc
        raise
    return review_id
