import hashlib
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from mira.models import (
        InformationRequirement,
        PartyContact,
        RequirementRequest,
        RequirementResolution,
        Supplier,
    )
    from mira.workflows.common import log_event

REQUESTED_SIGNAL = "requirement_requested"
RESOLVED_SIGNAL = "requirement_resolved"
GATE_DEADLINE_DAYS = 14
MIN_GATE_DEADLINE = timedelta(minutes=30)


def requirement_task_id(workflow_id: str, source_call_id: str) -> str:
    return "requirement-" + hashlib.sha256(f"{workflow_id}:{source_call_id}".encode()).hexdigest()[:24]


class RequirementGate:
    """Pause a child workflow at the exact request that needs another party's input, then resume that step.

    The owning workflow must forward the `requirement_resolved` signal to `resolve`.
    """

    def __init__(self) -> None:
        self.results: dict[str, RequirementResolution] = {}

    def resolve(self, result: RequirementResolution) -> None:
        if result.task_id not in self.results:
            self.results[result.task_id] = result

    async def wait(self, case_id: str, requester: PartyContact, source_call_id: str,
                   requirement: InformationRequirement, seconds_per_day: float) -> RequirementResolution:
        parent = workflow.info().parent
        if parent is None:
            return RequirementResolution(task_id="unowned", cleared=False, reason="No case owner to coordinate this request")
        task_id = requirement_task_id(workflow.info().workflow_id, source_call_id)
        request = RequirementRequest(task_id=task_id, case_id=case_id,
            requester_workflow_id=workflow.info().workflow_id, source_call_id=source_call_id,
            requester=requester, requirement=requirement)
        await log_event(case_id, "requirement_paused", task_id=task_id, requester=requester.name,
                        role=requester.role, source_call_id=source_call_id, description=requirement.description)
        await workflow.get_external_workflow_handle(parent.workflow_id).signal(REQUESTED_SIGNAL, request)
        deadline = max(timedelta(days=GATE_DEADLINE_DAYS * seconds_per_day / 86400), MIN_GATE_DEADLINE)
        try:
            await workflow.wait_condition(lambda: task_id in self.results, timeout=deadline)
        except TimeoutError:
            return RequirementResolution(task_id=task_id, cleared=False, reason="Requirement follow-up exceeded its deadline")
        result = self.results[task_id]
        await log_event(case_id, "requirement_resumed", task_id=task_id, cleared=result.cleared,
                        reference=result.accepted_reference, evidence=result.evidence_event_id)
        return result


def supplier_contact(supplier: Supplier) -> PartyContact:
    return PartyContact(role="supplier", name=supplier.supplier_name, phone=supplier.phone)
