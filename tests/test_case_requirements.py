"""The PCP's "needs info before signing" and the patient's questions are worked by the case coordinator.

Real CaseWorkflow, coordinator workflow, JSON store and gates on Temporal's time-skipping server;
scripted calls, keyword extraction and a scripted planner that still goes through the Deep Agents tool.
"""

import re
from uuid import uuid4

from langchain_core.messages import AIMessage
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from mira.activities import CallActivities
from mira.case_activities import CaseActivities
from mira.config import CASE_TASK_QUEUE, SCENARIOS_DIR, VOICE_CALL_TASK_QUEUE
from mira.coordinator.activities import CoordinatorActivities
from mira.coordinator.agent import propose_next_action
from mira.coordinator.communication import CommunicationActivities
from mira.coordinator.models import (
    ActionKind,
    CaseDocument,
    Party,
    ProposedAction,
    TaskStatus,
)
from mira.coordinator.store import JsonCaseStore
from mira.data import load_case
from mira.escalation_store import EscalationStore
from mira.events import EventLog
from mira.extraction import (
    CoordinationExtraction,
    FollowupExtraction,
    PatientExtraction,
    PcpExtraction,
    SupplierExtraction,
)
from mira.mock_fax import MockFaxInbox
from mira.models import (
    CallDisposition,
    CallPurpose,
    CaseInput,
    CaseOutcome,
    CaseSummary,
    CoordinationObservation,
    DeliveryStatus,
    HumanAction,
    HumanDecision,
    InformationRequirement,
    PatientUpdateResult,
    PcpOrderStatus,
)
from mira.voice.agent import MockVoiceAgent, RawCall, Scenario, load_scenario
from mira.worker import WORKFLOWS, case_activity_methods
from mira.workflows.case import CaseWorkflow
from mira.workflows.human_review import HumanReviewWorkflow
from tests.fakes import CONFIDENT, followup, patient, pcp, supplier
from tests.test_coordinator import ToolCallingModel

PATIENT = load_case().patient
PCP_PHONE = load_case().pcp.phone
REPORT = "pt-eval-1027"
REPORT_TYPE = "physical therapy mobility evaluation"
QUESTION = "Patient: Twenty percent of what, though? What will I actually owe in dollars?"
COST_ANSWER = ("Supplier: For Eleanor Martinez's K0001 the Medicare-approved amount is about $300, so her 20 percent "
               "is about $60, billed after delivery. We accept assignment.")
PATIENT_ACCEPTS = "Patient: About sixty dollars after delivery. Okay, that answers my question, thank you."


def connected(transcript: str) -> RawCall:
    return RawCall(disposition=CallDisposition.CONNECTED, transcript=transcript)


def with_calls(scenario: Scenario, phone: str, **calls: list[RawCall]) -> Scenario:
    party = scenario.parties[phone]
    updated = party.model_copy(update={"calls": {**party.calls, **{CallPurpose(k): v for k, v in calls.items()}}})
    return scenario.model_copy(update={"parties": {**scenario.parties, phone: updated}})


def pcp_scenario() -> Scenario:
    return load_scenario(SCENARIOS_DIR / "pcp_information.json")


def patient_question_scenario(supplier_answers: bool = True) -> Scenario:
    """PCP signs straight away; the patient asks a cost question on the plan call."""
    scenario = pcp_scenario()
    scenario = with_calls(scenario, PCP_PHONE, pcp_order_request=[connected("Office: Dr. Chen signed it and I'm faxing it now.")])
    scenario = with_calls(scenario, PATIENT.phone,
                          patient_update=[connected(QUESTION), connected("Patient: No questions, thank you dear.")],
                          coordination=[connected(PATIENT_ACCEPTS)])
    for phone, party in scenario.parties.items():
        if CallPurpose.SUPPLIER_SCREEN in party.calls:
            scenario = with_calls(scenario, phone, coordination=[connected(COST_ANSWER)] if supplier_answers else [])
    return scenario


def make_extraction(pcp_clinical_scope: bool | None = False):
    async def extraction(system: str, user: str, schema: type):
        transcript = user.split("Transcript:\n")[-1]
        if schema is CoordinationExtraction:
            return CoordinationExtraction(result=_coordination(user, transcript), confidence=CONFIDENT)
        if schema is PcpExtraction:
            return _pcp(transcript, pcp_clinical_scope)
        if schema is SupplierExtraction:
            return supplier(True, True, True, 2, accepts_assignment=True)
        if schema is FollowupExtraction:
            return followup(DeliveryStatus.SCHEDULED, eta=2)
        if schema is PatientExtraction:
            return _patient_update(transcript)
        raise AssertionError(schema)
    return extraction


def _coordination(user: str, transcript: str) -> CoordinationObservation:
    forwarded = re.search(r"Forward reference (\S+) for", user)
    if "role: patient" in user and REPORT in transcript:
        return CoordinationObservation(document_reference=REPORT, document_type=REPORT_TYPE,
                                       document_patient_name=PATIENT.name, sharing_authorized=True)
    if "role: patient" in user:
        return CoordinationObservation(requester_accepted=True, accepted_reference=forwarded.group(1))
    if "role: pcp" in user:
        return CoordinationObservation(requester_accepted=True, accepted_reference=REPORT)
    return CoordinationObservation(information="Approved amount about $300; her 20% is about $60, billed after delivery")


def _pcp(transcript: str, clinical_scope: bool | None) -> PcpExtraction:
    if "wants a copy" in transcript:
        result = pcp(PcpOrderStatus.NEEDS_INFO)
        request = InformationRequirement(description="Copy of the existing Lakeview PT mobility evaluation for the chart",
                                         document_type=REPORT_TYPE, clinical_decision_required=clinical_scope)
        return result.model_copy(update={"result": result.result.model_copy(update={"information_request": request})})
    if "faxing it now" in transcript:
        return pcp(PcpOrderStatus.SIGNED_AND_SENT)
    return pcp(PcpOrderStatus.IN_PROGRESS)


def _patient_update(transcript: str) -> PatientExtraction:
    if "What will I actually owe" not in transcript:
        return patient(True)
    return PatientExtraction(result=PatientUpdateResult(
        understood_next_steps=True, understood_cost_share=False, open_questions=["What will I owe in dollars?"], notes="",
        information_request=InformationRequirement(description="Estimated dollar amount of her 20% share",
                                                   clinical_decision_required=False),
    ), confidence=CONFIDENT)


async def scripted_planner(doc: CaseDocument) -> ProposedAction:
    """Ask a non-requester source, forward once the patient has confirmed, complete on explicit acceptance."""
    task = next(t for t in doc.tasks.values() if t.status is TaskStatus.READY)
    req = doc.knowledge.requirements[task.task_id]
    requester = Party(req.requester_role)
    base = {"task_id": task.task_id, "reason": "Scripted next coordination step"}
    if req.status == "accepted":
        action = ProposedAction(**base, kind=ActionKind.COMPLETE, evidence_event_id=req.accepted_event_id)
    elif req.current_reference and req.patient_confirmed_reference == req.current_reference:
        provided = next(e for e in reversed(doc.events) if e["task_id"] == task.task_id and (
            e["event_id"] == req.current_reference
            or (e.get("observation") or {}).get("document_reference") == req.current_reference))
        action = ProposedAction(**base, kind=ActionKind.FORWARD, party=requester, evidence_event_id=provided["event_id"],
                                message="Does this satisfy what you needed?")
    else:
        source = Party.SUPPLIER if requester is Party.PATIENT else Party.PATIENT
        action = ProposedAction(**base, kind=ActionKind.CONTACT, party=source, message="Can you provide what is needed?")
    model = ToolCallingModel(responses=[AIMessage(content="", tool_calls=[{
        "id": "next-step", "name": "propose_action", "args": action.model_dump(mode="json"),
    }])])
    return await propose_next_action(doc, model=model)


async def run_case(tmp_path, scenario: Scenario, extraction, close_reviews: bool = False):
    seen = []

    class RecordingVoice(MockVoiceAgent):
        async def call(self, request):
            seen.append(request)
            return await super().call(request)

    store = JsonCaseStore(tmp_path / "state")
    escalations = EscalationStore(tmp_path / "escalations.json")
    coordinator = CoordinatorActivities(store, scripted_planner)
    communication = CommunicationActivities(store)
    events = EventLog(tmp_path / "events.jsonl")
    cases = CaseActivities(events, MockFaxInbox(scenario), escalations)
    voice = CallActivities(RecordingVoice(scenario), events, complete=extraction)
    async with await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env:
        async with (
            Worker(env.client, task_queue=CASE_TASK_QUEUE, workflows=WORKFLOWS, activities=[
                *case_activity_methods(cases), coordinator.sync, coordinator.plan, coordinator.apply, coordinator.failure,
                communication.prepare_communication, communication.record_communication, communication.communication_failed,
            ]),
            Worker(env.client, task_queue=VOICE_CALL_TASK_QUEUE, activities=[voice.place_call]),
        ):
            handle = await env.client.start_workflow(
                CaseWorkflow.run, CaseInput(case=load_case(), seconds_per_day=60),
                id=f"requirements-{uuid4()}", task_queue=CASE_TASK_QUEUE,
            )
            if close_reviews:
                await _close_first_review(env, handle)
            summary: CaseSummary = await handle.result()
    docs = list((tmp_path / "state").glob("*.json"))
    doc = store.read(docs[0].stem) if docs else None
    return summary, doc, seen, escalations


async def _close_first_review(env, handle) -> None:
    for _ in range(200):
        state = await handle.query(CaseWorkflow.status)
        if state.pending_reviews:
            review = state.pending_reviews[0]
            await env.client.get_workflow_handle(review.review_workflow_id).signal(
                HumanReviewWorkflow.decide,
                HumanDecision(escalation_id=review.escalation_id, action=HumanAction.CLOSE, note="Needs a clinician"))
            return
        await env.sleep(1)
    raise AssertionError("Case never reached human review")


def coordination_roles(seen) -> list[str]:
    return [r.coordination_role for r in seen if r.purpose is CallPurpose.COORDINATION]


async def test_pcp_needs_info_is_resolved_by_coordinator_and_order_chase_resumes(tmp_path):
    summary, doc, seen, _ = await run_case(tmp_path, pcp_scenario(), make_extraction())

    assert summary.outcome is CaseOutcome.DELIVERY_SCHEDULED
    assert not any("pcp-needs-info" in e for e in summary.escalations)
    requirement = next(iter(doc.knowledge.requirements.values()))
    assert requirement.requester_role == "pcp"
    assert requirement.accepted_reference == REPORT
    assert coordination_roles(seen) == ["patient", "pcp"]
    nudges = [r for r in seen if r.purpose is CallPurpose.PCP_ORDER_NUDGE]
    assert any(REPORT in point for point in nudges[-1].talking_points)  # the resumed chase cites what was accepted
    assert summary.order.order.hcpcs_code == "K0001"


async def test_clinical_pcp_request_goes_to_human_without_coordination_calls(tmp_path):
    summary, doc, seen, _ = await run_case(
        tmp_path, pcp_scenario(), make_extraction(pcp_clinical_scope=None), close_reviews=True)

    assert summary.outcome is CaseOutcome.CLOSED_BY_HUMAN
    assert any(e.endswith(":pcp-needs-info") for e in summary.escalations)
    assert coordination_roles(seen) == []
    assert all(task.status is TaskStatus.REVIEW for task in doc.tasks.values())


async def test_patient_question_is_answered_by_supplier_without_a_callback(tmp_path):
    summary, doc, seen, _ = await run_case(tmp_path, patient_question_scenario(), make_extraction())

    assert summary.outcome is CaseOutcome.DELIVERY_SCHEDULED
    requirement = next(iter(doc.knowledge.requirements.values()))
    assert requirement.requester_role == "patient"
    assert requirement.status == "accepted"
    assert coordination_roles(seen) == ["supplier", "patient"]
    plan_contact = next(c for c in summary.patient_contacts if c.milestone == "plan")
    assert plan_contact.requirement_task_id and plan_contact.escalation_id is None
    assert not any("patient-questions" in e for e in summary.escalations)


async def test_unanswered_patient_question_falls_back_to_a_callback_and_case_still_finishes(tmp_path):
    summary, _, _, escalations = await run_case(
        tmp_path, patient_question_scenario(supplier_answers=False), make_extraction())

    assert summary.outcome is CaseOutcome.DELIVERY_SCHEDULED
    callback_ids = [e for e in summary.escalations if e.endswith(":patient-questions-plan")]
    assert callback_ids
    callback = escalations.get_escalation(callback_ids[0])
    assert callback.context["blocking"] == "false"
    assert "Estimated dollar amount" in callback.context["questions"]
