"""Prepare calls and reconcile their observations using the same typed inbox as future chat/email."""

from temporalio import activity
from temporalio.exceptions import ApplicationError

from mira.coordinator.models import (
    ActionKind, CaseDocument, CommunicationRequest, CommunicationResult, EventKind, IncomingEvent, Party,
    Snapshot, SyncRequest, CoordinatorInput,
    TaskStatus,
)
from mira.coordinator.store import CoordinatorError, JsonCaseStore, snapshot
from mira.models import CallDisposition, CallPurpose, CallRequest, RequirementRequest
from mira.policy import MIN_EXTRACTION_CONFIDENCE


class CommunicationActivities:
    def __init__(self, store: JsonCaseStore | None = None) -> None:
        self.store = store or JsonCaseStore()

    @activity.defn
    async def prepare_communication(self, request: CommunicationRequest) -> CallRequest | None:
        doc = self.store.read(request.case_id)
        action = doc.turns.get(request.action_id)
        if action is None:
            return None  # The turn committed nothing (e.g. a fallback review found no ready task), so there is no call.
        task = doc.tasks[action["task_id"]]
        source = task.requirement_request
        if source is None or action.get("not_sent") or action["kind"] not in {ActionKind.CONTACT, ActionKind.FORWARD}:
            return None
        if request.action_id in doc.communication_results:
            return None
        party = Party(action["party"])
        name, phone = _contact_for(doc, source, party)
        requirement = doc.knowledge.requirements[task.task_id]
        points = [
            f"Requirement {task.task_id}: {requirement.description}",
            f"Patient: {doc.patient_name}; requested by {source.requester.name} ({source.requester.role})",
            action["message"] or action["reason"],
            f"Current reference: {requirement.current_reference}; confirmed by patient: {requirement.patient_confirmed_reference}",
        ]
        if action["kind"] == ActionKind.FORWARD:
            evidence = next((event for event in doc.events
                             if event["event_id"] == action["evidence_event_id"]), None)
            information = (evidence or {}).get("observation", {}).get("information")
            if information:
                points.append(f"Patient-authorized information for supplier review: {information}")
            points.extend([
                f"Forward reference {requirement.forwarded_reference} for the requester's review; document contents have not been checked by this system",
                "Ask whether this exact reference satisfies the requirement; receipt alone is not acceptance",
                f"The called party IS the requester: if they say it satisfies or answers their request, that is acceptance "
                f"of reference {requirement.forwarded_reference}",
            ])
        return CallRequest(case_id=request.case_id, call_id=f"coordination:{request.action_id}",
            purpose=CallPurpose.COORDINATION, party_name=name, phone=phone,
            coordination_role=party.value, requirement_id=task.task_id, talking_points=points,
            attempt=1 + sum(
                doc.turns[key]["task_id"] == task.task_id and doc.turns[key].get("party") == party.value
                for key in doc.communication_results
            ))

    @activity.defn
    async def record_communication(self, result: CommunicationResult) -> Snapshot:
        request, outcome = result.request, result.outcome
        doc = self.store.read(request.case_id)
        if request.action_id in doc.communication_results:
            return snapshot(doc)
        action = doc.turns[request.action_id]
        task = doc.tasks[action["task_id"]]
        if outcome.call_id != f"coordination:{request.action_id}" or outcome.purpose is not CallPurpose.COORDINATION:
            raise ApplicationError("Call result does not match this action", non_retryable=True)
        # First commit the provider result. Retries can reapply the same event ID safely.
        if outcome.disposition is CallDisposition.CONNECTED and outcome.confidence >= MIN_EXTRACTION_CONFIDENCE and outcome.coordination:
            event = IncomingEvent(event_id=outcome.call_id, task_id=task.task_id,
                party=Party(action["party"]), kind=EventKind.OBSERVATION,
                text=outcome.transcript or outcome.coordination.notes or "Connected call without stated facts",
                observation=outcome.coordination)
            try:
                self.store.sync(SyncRequest(inp=CoordinatorInput(case_id=doc.case_id, patient_name=doc.patient_name, tasks=[]),
                                            now=result.now, events=[event]))
            except CoordinatorError as exc:
                raise ApplicationError(str(exc), non_retryable=True) from exc

        def finish(current):
            return current.model_copy(update={"communication_results": {**current.communication_results, request.action_id: outcome}})
        return snapshot(self.store.change(request.case_id, finish))

    @activity.defn
    async def communication_failed(self, request: CommunicationRequest) -> Snapshot:
        def fail(doc):
            action = doc.turns.get(request.action_id)
            if action is None or request.action_id in doc.communication_results:
                return doc
            task = doc.tasks[action["task_id"]]
            if task.status.value in {"completed", "cancelled"}:
                return doc
            task = task.model_copy(update={"status": TaskStatus.REVIEW, "next_check_at": None,
                "review_reason": "Communication failed after retries; reconcile the call outcome before proceeding"})
            return doc.model_copy(update={"tasks": {**doc.tasks, task.task_id: task}})
        return snapshot(self.store.change(request.case_id, fail))


def _contact_for(doc: CaseDocument, source: RequirementRequest, party: Party) -> tuple[str, str]:
    """The requester, then contacts attached to this request, then the case-wide contacts."""
    for contact in (source.requester, *source.other_contacts):
        if contact.role == party.value:
            return contact.name, contact.phone
    contact = doc.contacts.get(party.value)
    if contact is None:
        raise ApplicationError(f"No authorized contact for {party}", non_retryable=True)
    return contact.name, contact.phone
