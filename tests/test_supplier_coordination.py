import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from mira.activities import CallActivities
from mira.case_activities import CaseActivities
from mira.config import CASE_TASK_QUEUE, DATA_DIR, VOICE_CALL_TASK_QUEUE
from mira.coordinator.activities import CoordinatorActivities
from mira.coordinator.agent import propose_next_action
from mira.coordinator.communication import CommunicationActivities
from mira.coordinator.models import (
    ActionKind, CommunicationRequest, CoordinatorInput, EventKind, FollowupTask, IncomingEvent, Party, ProposedAction,
    SyncRequest, TaskKind, TaskStatus, TurnRequest,
)
from mira.coordinator.store import CoordinatorError, JsonCaseStore
from mira.data import load_case, load_suppliers
from mira.escalation_store import EscalationStore
from mira.events import EventLog
from mira.extraction import CoordinationExtraction, FollowupExtraction, PatientExtraction, PcpExtraction, SupplierExtraction
from mira.mock_fax import MockFaxInbox
from mira.models import (
    CallDisposition, CallPurpose, CaseInput, CaseOutcome, CoordinationObservation, DeliveryStatus,
    HumanAction, HumanDecision, InformationRequirement, PcpOrderStatus, RequirementRequest,
)
from mira.voice.agent import MockVoiceAgent, RawCall, load_scenario
from mira.worker import WORKFLOWS, case_activity_methods
from mira.workflows.case import CaseWorkflow
from mira.workflows.human_review import HumanReviewWorkflow
from mira.workflows.requirement_gate import supplier_contact
from tests.fakes import followup, patient, pcp, supplier
from tests.test_coordinator import ToolCallingModel

NOW = datetime(2026, 11, 18, tzinfo=UTC)


def new_store(tmp_path):
    case = load_case()
    request = RequirementRequest(task_id="requirement", case_id=case.case_id,
        requester_workflow_id="case/sourcing", source_call_id="source-call", requester=supplier_contact(load_suppliers()[0]),
        requirement=InformationRequirement(description="Need the mobility assessment report", document_type="mobility assessment report",
                                        clinical_decision_required=False))
    inp = CoordinatorInput(case_id="test-knowledge", patient_name=case.patient.name,
        tasks=[FollowupTask(task_id=request.task_id, kind=TaskKind.REQUIREMENT, goal=request.requirement.description,
                            requirement_request=request)])
    store = JsonCaseStore(tmp_path)
    store.sync(SyncRequest(inp=inp, now=NOW))
    return store, inp


def observation(store, inp, event_id, party, **data):
    event = IncomingEvent(event_id=event_id, task_id="requirement", party=party, kind=EventKind.OBSERVATION,
                          text="Scripted source observation", observation=CoordinationObservation(**data))
    return store.sync(SyncRequest(inp=inp, now=NOW, events=[event]))


def apply(store, inp, kind, evidence=None, party=None):
    return store.apply(TurnRequest(case_id=inp.case_id, turn_id=str(uuid4()), now=NOW, reminder_seconds=60),
        store.read(inp.case_id).version, ProposedAction(task_id="requirement", kind=kind, reason="Policy test",
                                                     party=party, evidence_event_id=evidence))


def test_document_cannot_be_forwarded_without_exact_patient_permission(tmp_path):
    store, inp = new_store(tmp_path)
    observation(store, inp, "provided", Party.PCP, document_reference="doc-1", document_type="mobility assessment report",
                document_patient_name=load_case().patient.name)
    with pytest.raises(CoordinatorError, match="Patient must confirm"):
        apply(store, inp, ActionKind.FORWARD, "provided", Party.SUPPLIER)


def test_receipt_or_acceptance_of_wrong_reference_does_not_clear_requirement(tmp_path):
    store, inp = new_store(tmp_path)
    observation(store, inp, "provided", Party.PCP, document_reference="doc-1", document_type="mobility assessment report",
                document_patient_name=load_case().patient.name)
    observation(store, inp, "permission", Party.PATIENT, confirmed_reference="doc-1", sharing_authorized=True)
    apply(store, inp, ActionKind.FORWARD, "provided", Party.SUPPLIER)
    observation(store, inp, "receipt", Party.SUPPLIER, notes="Received; not reviewed", accepted_reference="doc-1")
    with pytest.raises(CoordinatorError, match="explicitly accept"):
        apply(store, inp, ActionKind.COMPLETE, "receipt")
    observation(store, inp, "wrong-acceptance", Party.SUPPLIER, requester_accepted=True, accepted_reference="other-doc")
    with pytest.raises(CoordinatorError, match="explicitly accept"):
        apply(store, inp, ActionKind.COMPLETE, "wrong-acceptance")


def test_new_document_invalidates_old_permission_and_supplier_acceptance(tmp_path):
    store, inp = new_store(tmp_path)
    for ref in ("doc-1",):
        observation(store, inp, "provided", Party.PCP, document_reference=ref, document_type="mobility assessment report",
                    document_patient_name=load_case().patient.name)
        observation(store, inp, "permission", Party.PATIENT, confirmed_reference=ref, sharing_authorized=True)
        apply(store, inp, ActionKind.FORWARD, "provided", Party.SUPPLIER)
        observation(store, inp, "accepted", Party.SUPPLIER, requester_accepted=True, accepted_reference=ref)
    observation(store, inp, "replacement", Party.PCP, document_reference="doc-2", document_type="mobility assessment report",
                document_patient_name=load_case().patient.name)
    requirement = store.read(inp.case_id).knowledge.requirements["requirement"]
    assert requirement.current_reference == "doc-2"
    assert requirement.patient_confirmed_reference is None
    assert requirement.accepted_reference is None
    with pytest.raises(CoordinatorError):
        apply(store, inp, ActionKind.COMPLETE, "accepted")


@pytest.mark.parametrize("data", [
    {"document_reference": "wrong", "document_type": "mobility assessment report", "document_patient_name": "Another person"},
    {"clinical_decision_required": True},
])
def test_wrong_identity_or_clinical_judgment_requires_review(tmp_path, data):
    store, inp = new_store(tmp_path)
    state = observation(store, inp, "unsafe", Party.PCP, **data)
    assert state.tasks[0].status is TaskStatus.REVIEW


@pytest.mark.parametrize("clinical_scope", [True, None])
def test_clinical_or_unspecified_source_requirement_never_starts_autonomous_contacts(tmp_path, clinical_scope):
    _, inp = new_store(tmp_path / "initial")
    task = inp.tasks[0]
    source = task.requirement_request
    source = source.model_copy(update={"requirement": source.requirement.model_copy(
        update={"clinical_decision_required": clinical_scope})})
    inp = inp.model_copy(update={"tasks": [task.model_copy(update={"requirement_request": source})]})
    store = JsonCaseStore(tmp_path / "guarded")
    state = store.sync(SyncRequest(inp=inp, now=NOW))
    assert state.tasks[0].status is TaskStatus.REVIEW
    assert not state.ready
    assert not store.read(inp.case_id).actions


async def test_forwarding_plain_information_includes_the_authorized_source_facts(tmp_path):
    store, inp = new_store(tmp_path)
    def information_only(doc):
        task = doc.tasks["requirement"]
        source = task.requirement_request
        source = source.model_copy(update={"requirement": source.requirement.model_copy(update={"document_type": None})})
        return doc.model_copy(update={"tasks": {task.task_id: task.model_copy(update={"requirement_request": source})}})
    store.change(inp.case_id, information_only)
    observation(store, inp, "provided-info", Party.PCP, information="The office fax number is 3125550100")
    observation(store, inp, "permission", Party.PATIENT, confirmed_reference="provided-info", sharing_authorized=True)
    state = apply(store, inp, ActionKind.FORWARD, "provided-info", Party.SUPPLIER)
    call = await CommunicationActivities(store).prepare_communication(
        CommunicationRequest(case_id=inp.case_id, action_id=state.last_action["action_id"]))
    assert any("3125550100" in point for point in call.talking_points)
    assert call.phone == inp.tasks[0].requirement_request.requester.phone


def test_information_cannot_substitute_for_a_requested_document(tmp_path):
    store, inp = new_store(tmp_path)
    observation(store, inp, "provided-info", Party.PATIENT, information="I attended therapy", sharing_authorized=True)
    with pytest.raises(CoordinatorError, match="document requirement"):
        apply(store, inp, ActionKind.FORWARD, "provided-info", Party.SUPPLIER)


async def offline_planner(doc):
    task = next(t for t in doc.tasks.values() if t.status is TaskStatus.READY)
    req = doc.knowledge.requirements[task.task_id]
    base = {"task_id": task.task_id, "reason": "Scripted model chooses the next multi-party coordination step"}
    if req.status == "accepted":
        action = ProposedAction(**base, kind=ActionKind.COMPLETE, evidence_event_id=req.accepted_event_id)
    elif req.status in {"awaiting_provider", "requester_rejected"}:
        action = ProposedAction(**base, kind=ActionKind.CONTACT, party=Party.PCP, message="Please provide or correct the requested document reference")
    elif req.current_reference is None or req.patient_confirmed_reference != req.current_reference:
        action = ProposedAction(**base, kind=ActionKind.CONTACT, party=Party.PATIENT,
                                message=f"Please provide or confirm {req.current_reference} and sharing permission")
    else:
        provided = next(e for e in reversed(doc.events) if e["task_id"] == task.task_id and
            e.get("observation") and e["observation"].get("document_reference") == req.current_reference)
        action = ProposedAction(**base, kind=ActionKind.FORWARD, party=Party.SUPPLIER,
                                evidence_event_id=provided["event_id"], message="Please verify and explicitly accept this reference")
    model = ToolCallingModel(responses=[AIMessage(content="", tool_calls=[{
        "id": "next-step", "name": "propose_action", "args": action.model_dump(mode="json"),
    }])])
    return await propose_next_action(doc, model=model)


async def extraction(system, user, schema):
    transcript = user.split("Transcript:\n")[-1]
    if schema is CoordinationExtraction:
        ref = re.search(r"doc-ref-\d+", transcript)
        reference = ref.group() if ref else None
        if "role: patient" in user:
            data = (CoordinationObservation(needs_provider=True) if "do not have" in transcript else
                    CoordinationObservation(confirmed_reference=reference, sharing_authorized=True))
        elif "role: pcp" in user:
            data = CoordinationObservation(document_reference=reference, document_type="mobility assessment report",
                                           document_patient_name=load_case().patient.name)
        else:
            data = CoordinationObservation(requester_accepted="do not accept" not in transcript, accepted_reference=reference)
        return CoordinationExtraction(result=data, confidence=.95)
    request = InformationRequirement(description="Please provide the mobility assessment report", document_type="mobility assessment report",
                                  clinical_decision_required=False) if "Before proceeding" in transcript else None
    if schema is SupplierExtraction:
        result = supplier(True, True, True, 2, accepts_assignment=True)
        return result.model_copy(update={"result": result.result.model_copy(update={"information_request": request})})
    if schema is FollowupExtraction:
        result = followup(DeliveryStatus.NOT_SCHEDULED if request else DeliveryStatus.SCHEDULED, eta=2)
        return result.model_copy(update={"result": result.result.model_copy(update={"information_request": request})})
    if schema is PcpExtraction:
        return pcp(PcpOrderStatus.SIGNED_AND_SENT)
    if schema is PatientExtraction:
        return patient(True)
    raise AssertionError(schema)


async def execute_case(tmp_path, phase, rejected=False, clinical_scope=False):
    scenario = load_scenario(DATA_DIR / "scenarios" / "supplier_information.json")
    case = load_case()
    primary = load_suppliers()[0]
    party = scenario.parties[primary.phone]
    calls = dict(party.calls)
    source_request = calls[CallPurpose.SUPPLIER_SCREEN][0]
    if phase == "fulfillment":
        calls[CallPurpose.SUPPLIER_SCREEN] = calls[CallPurpose.SUPPLIER_SCREEN][1:]
    if phase in {"fulfillment", "both"}:
        calls[CallPurpose.SUPPLIER_FOLLOWUP] = [source_request, *calls[CallPurpose.SUPPLIER_FOLLOWUP]]
    parties = {**scenario.parties, primary.phone: party.model_copy(update={"calls": calls})}
    if rejected:
        calls[CallPurpose.COORDINATION] = [
            RawCall(disposition=CallDisposition.CONNECTED, transcript="Supplier: We do not accept doc-ref-001; please obtain the corrected document from the PCP."),
            RawCall(disposition=CallDisposition.CONNECTED, transcript="Supplier: We explicitly accept doc-ref-002 and it satisfies the requirement."),
        ]
        for phone in (case.pcp.phone, case.patient.phone):
            current = parties[phone]
            extra = current.calls[CallPurpose.COORDINATION][-1].model_copy(update={
                "transcript": current.calls[CallPurpose.COORDINATION][-1].transcript.replace("doc-ref-001", "doc-ref-002")})
            parties[phone] = current.model_copy(update={"calls": {**current.calls,
                CallPurpose.COORDINATION: [*current.calls[CallPurpose.COORDINATION], extra]}})
    scenario = scenario.model_copy(update={"parties": parties})
    seen = []

    class Voice(MockVoiceAgent):
        async def call(self, request):
            result = await super().call(request)
            seen.append((request, result))
            return result

    store = JsonCaseStore(tmp_path / "state")
    coordinator = CoordinatorActivities(store, offline_planner)
    communication = CommunicationActivities(store)
    events = EventLog(tmp_path / "events.jsonl")
    cases = CaseActivities(events, MockFaxInbox(scenario), EscalationStore(tmp_path / "escalations.json"))
    async def scoped_extraction(system, user, schema):
        result = await extraction(system, user, schema)
        if schema in {SupplierExtraction, FollowupExtraction} and result.result.information_request:
            requirement = result.result.information_request.model_copy(update={"clinical_decision_required": clinical_scope})
            result = result.model_copy(update={"result": result.result.model_copy(update={"information_request": requirement})})
        return result
    voice = CallActivities(Voice(scenario), events, complete=scoped_extraction)
    async with await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env:
        async with (
            Worker(env.client, task_queue=CASE_TASK_QUEUE, workflows=WORKFLOWS, activities=[
                *case_activity_methods(cases), coordinator.sync, coordinator.plan, coordinator.apply, coordinator.failure,
                communication.prepare_communication, communication.record_communication, communication.communication_failed,
            ]),
            Worker(env.client, task_queue=VOICE_CALL_TASK_QUEUE, activities=[voice.place_call], max_concurrent_activities=1),
        ):
            root_id = "midrun-" + str(uuid4())
            handle = await env.client.start_workflow(CaseWorkflow.run, CaseInput(case=case, seconds_per_day=60),
                                                     id=root_id, task_queue=CASE_TASK_QUEUE)
            if clinical_scope is not False:
                for _ in range(200):
                    state = await handle.query(CaseWorkflow.status)
                    if state.pending_reviews:
                        review = state.pending_reviews[0]
                        await env.client.get_workflow_handle(review.review_workflow_id).signal(
                            HumanReviewWorkflow.decide, HumanDecision(escalation_id=review.escalation_id,
                                action=HumanAction.CLOSE, note="Clinical requirement routed to care team"))
                        break
                    await env.sleep(1)
                else:
                    raise AssertionError("Clinical supplier request never reached human review")
            summary = await handle.result()
            suffixes = ("", "/coordinator", "/sourcing", "/pcp-order")
            if clinical_scope is False:
                suffixes += ("/fulfillment",)
            for suffix in suffixes:
                history = await env.client.get_workflow_handle(root_id + suffix).fetch_history()
                (tmp_path / (suffix.replace("/", "") or "case")).with_suffix(".history.json").write_text(history.to_json())
    docs = list((tmp_path / "state").glob("*.json"))
    assert len(docs) == 1  # One coordinator and canonical graph across sourcing and fulfillment.
    doc = store.read(docs[0].stem)
    return summary, doc, seen


@pytest.mark.parametrize("phase", ["sourcing", "fulfillment", "both"])
async def test_supplier_can_pause_either_workflow_for_patient_pcp_patient_supplier_loop(tmp_path, phase):
    summary, doc, seen = await execute_case(tmp_path, phase)
    assert summary.outcome is CaseOutcome.DELIVERY_SCHEDULED
    requirements = list(doc.knowledge.requirements.values())
    assert len(requirements) == (2 if phase == "both" else 1)
    assert all(r.status == "accepted" and r.accepted_reference == "doc-ref-001" for r in requirements)
    roles = [r.coordination_role for r, _ in seen if r.purpose is CallPurpose.COORDINATION]
    assert roles == ["patient", "pcp", "patient", "supplier"] * len(requirements)
    assert doc.knowledge.documents["doc-ref-001"].verification_status == "not_checked"
    assert all(t.status is TaskStatus.COMPLETED for t in doc.tasks.values())
    assert doc.closed


async def test_supplier_rejection_routes_back_to_pcp_then_patient_before_resubmission(tmp_path):
    summary, doc, seen = await execute_case(tmp_path, "sourcing", rejected=True)
    assert summary.outcome is CaseOutcome.DELIVERY_SCHEDULED
    requirement = next(iter(doc.knowledge.requirements.values()))
    assert requirement.accepted_reference == "doc-ref-002"
    roles = [r.coordination_role for r, _ in seen if r.purpose is CallPurpose.COORDINATION]
    assert roles == ["patient", "pcp", "patient", "supplier", "pcp", "patient", "supplier"]


@pytest.mark.parametrize("clinical_scope", [True, None])
async def test_clinical_or_unclear_supplier_requirement_blocks_dme_and_reaches_human(tmp_path, clinical_scope):
    summary, doc, seen = await execute_case(tmp_path, "sourcing", clinical_scope=clinical_scope)
    assert summary.outcome is CaseOutcome.CLOSED_BY_HUMAN
    assert all(task.status is TaskStatus.REVIEW for task in doc.tasks.values())
    assert not any(request.purpose in {CallPurpose.COORDINATION, CallPurpose.SUPPLIER_FOLLOWUP}
                   for request, _ in seen)
    assert doc.closed
