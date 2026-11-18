from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from mira.models import Escalation, HumanDecision, HumanReviewInput

REVIEW_SUFFIX = "/human-review"


def review_workflow_id(escalation_id: str) -> str:
    return f"{escalation_id}{REVIEW_SUFFIX}"


def _waiting_details(escalation: Escalation) -> str:
    context = "\n".join(f"- {k}: {v}" for k, v in escalation.context.items() if k != "blocking")
    return (
        f"⏸ **Waiting for a care advocate**\n\n{escalation.reason}\n\n{context}\n\n"
        f"Resolve with `uv run python -m mira.human decide {escalation.escalation_id} retry|close`"
    )


@workflow.defn
class HumanReviewWorkflow:
    """A visible, durable 'human task': runs until a care advocate signals a decision."""

    def __init__(self) -> None:
        self._request: Escalation | None = None
        self._decision: HumanDecision | None = None

    @workflow.run
    async def run(self, inp: HumanReviewInput) -> HumanDecision:
        self._request = inp.escalation
        workflow.set_current_details(_waiting_details(inp.escalation))
        await workflow.wait_condition(lambda: self._decision is not None)
        decision = self._decision
        workflow.set_current_details(f"✅ Resolved: **{decision.action}** — {decision.note or 'no note'}")
        return decision

    @workflow.signal
    def decide(self, decision: HumanDecision) -> None:
        if self._decision is None:
            self._decision = decision

    @workflow.query
    def request(self) -> Escalation | None:
        return self._request
