import json
from datetime import UTC, datetime
from pathlib import Path

from temporalio import activity

from mira.coordinator.evaluate import scenario_events
from mira.coordinator.models import IncomingEvent, Party, Snapshot, SyncRequest
from mira.coordinator.store import JsonCaseStore
from mira.data import load_suppliers
from mira.events import EventLog
from mira.models import (
    CallDisposition, CallPurpose, CallRequest, Case, Equipment, Patient, Pcp, WrittenOrder,
)
from mira.simulation.models import ActionRequest, SimulationEvent, SimulationInput, StageRequest
from mira.voice.agent import MockVoiceAgent, RawCall, Scenario, ScriptedParty

PHONES = {Party.PATIENT: "(312) 555-0100", Party.PCP: "(312) 555-0198", Party.SUPPLIER: "(312) 555-0142"}
PURPOSES = {Party.PATIENT: CallPurpose.PATIENT_UPDATE, Party.PCP: CallPurpose.PCP_ORDER_NUDGE,
            Party.SUPPLIER: CallPurpose.SUPPLIER_FOLLOWUP}


class SimulationTrace(EventLog):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.path = path

    def append(self, case_id: str, kind: str, payload: dict) -> None:
        wall = datetime.now(UTC).isoformat()
        workflow_id = None
        scheduled = None
        if activity.in_activity():
            info = activity.info()
            workflow_id = info.workflow_id
            scheduled = info.scheduled_time.isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "actual_time_utc": wall, "business_time_utc": payload.get("business_time_utc", scheduled),
            "case_id": case_id, "workflow_id": workflow_id, "kind": kind, **payload,
        }
        with self.path.open("a") as handle:
            handle.write(json.dumps(record, default=str) + "\n")


def demo_case(case_id: str) -> Case:
    return Case(
        case_id=case_id,
        patient=Patient(name="Demo User", age=70, phone=PHONES[Party.PATIENT], home_zip="60614",
                        coverage="Original Medicare Part B", has_supplemental=False),
        equipment=Equipment(description="Standard manual wheelchair", hcpcs_code="K0001"),
        pcp=Pcp(name="Demo PCP", practice="Demo Practice", city="Chicago, IL", phone=PHONES[Party.PCP]),
        pcp_visit_days_ago=3, order_status="followup_required",
        assumptions=["Entirely synthetic simulation; no real calls, patient data, bookings or equipment delivery"],
    )


def delivery_scenario(case: Case, now: datetime) -> Scenario:
    """A signed order and supplier service become available after the coordinator prerequisite completes."""
    def raw(text):
        return RawCall(disposition=CallDisposition.CONNECTED, transcript=text)

    parties = {
        s.phone: ScriptedParty(name=s.supplier_name, calls={
            CallPurpose.SUPPLIER_SCREEN: [raw(
                "Supplier: We are Medicare enrolled, accept new Medicare patients and assignment, have K0001 in stock, "
                "serve ZIP 60614, and explicitly agree to take this case. Delivery is two days after the signed order."
            )],
            CallPurpose.SUPPLIER_FOLLOWUP: [raw(
                "Supplier: We received the signed K0001 order. Delivery is booked on our schedule for two days from now."
            )],
        }) for s in load_suppliers()
    }
    parties[case.pcp.phone] = ScriptedParty(name=case.pcp.practice, calls={
        CallPurpose.PCP_ORDER_REQUEST: [raw(
            "Office: The requested administrative follow-up has been resolved. Demo PCP signed the K0001 written order "
            "and sent it to the supplier and coordinator."
        )],
        CallPurpose.PCP_ORDER_NUDGE: [raw("Office: The signed K0001 order has been sent.")],
    })
    parties[case.patient.phone] = ScriptedParty(name=case.patient.name, calls={
        CallPurpose.PATIENT_UPDATE: [raw(
            "Patient: I understand the next steps and the approximate cost share, conditional on assignment and the deductible. "
            "I have no questions. I will be available for the supplier delivery."
        )],
    })
    order = WrittenOrder(
        hcpcs_code="K0001", item_description="Standard manual wheelchair", quantity=1,
        patient_name=case.patient.name, prescriber_name=case.pcp.name, prescriber_npi=None,
        face_to_face_date=now.date(), order_date=now.date(), signed=True,
    )
    return Scenario(description="Synthetic post-follow-up DME fulfillment", parties=parties, written_orders=[order])


async def mock_call(case_id: str, party: Party, call_id: str, text: str, direction: str,
                    trace: SimulationTrace, business_time: datetime, no_answer: bool = False) -> RawCall:
    purpose = PURPOSES[party]
    phone = PHONES[party]
    raw = RawCall(disposition=CallDisposition.NO_ANSWER if no_answer else CallDisposition.CONNECTED,
                  transcript="" if no_answer else text)
    voice = MockVoiceAgent(Scenario(description="Scripted coordination conversation", parties={
        phone: ScriptedParty(name=f"Demo {party}", calls={purpose: [raw]}),
    }))
    request = CallRequest(case_id=case_id, call_id=call_id, purpose=purpose, party_name=f"Demo {party}",
                          phone=phone, talking_points=[text])
    trace.append(case_id, "mock_call_started", {
        "business_time_utc": business_time.isoformat(), "call_id": call_id,
        "party": party, "direction": direction, "talking_points": request.talking_points,
    })
    result = await voice.call(request)
    trace.append(case_id, "mock_call_completed", {
        "business_time_utc": business_time.isoformat(), "call_id": call_id,
        "party": party, "direction": direction, **result.model_dump(mode="json"),
    })
    return result


class TracedVoiceAgent:
    """Records real calls to the existing MockVoiceAgent without changing its interface."""
    def __init__(self, scenario: Scenario, trace: SimulationTrace) -> None:
        self.voice, self.trace = MockVoiceAgent(scenario), trace

    async def call(self, request: CallRequest) -> RawCall:
        self.trace.append(request.case_id, "mock_call_started", {
            "call_id": request.call_id, "party": request.party_name, "direction": "outbound_dme",
            "purpose": request.purpose, "talking_points": request.talking_points,
        })
        result = await self.voice.call(request)
        self.trace.append(request.case_id, "mock_call_completed", {
            "call_id": request.call_id, "party": request.party_name, "direction": "outbound_dme",
            "purpose": request.purpose, **result.model_dump(mode="json"),
        })
        return result


class SimulationActivities:
    def __init__(self, store: JsonCaseStore, trace: SimulationTrace) -> None:
        self.store, self.trace = store, trace

    @activity.defn
    async def prepare(self, inp: SimulationInput) -> Snapshot:
        now = activity.info().scheduled_time
        state = self.store.sync(SyncRequest(inp=inp.coordinator, now=now))
        self.trace.append(inp.case.case_id, "simulation_started", {
            "business_time_utc": now.isoformat(), "scenario": inp.scenario["id"], "goal": inp.coordinator.tasks[0].goal,
        })
        return state

    @activity.defn
    async def record(self, event: SimulationEvent) -> None:
        self.trace.append(event.case_id, event.kind, {
            "business_time_utc": event.business_time.isoformat(), **event.details,
        })

    @activity.defn
    async def deliver_stage(self, request: StageRequest) -> list[IncomingEvent]:
        stage = request.inp.scenario["stages"][request.stage_index]
        events = scenario_events(stage, request.business_time)
        for event in events:
            await mock_call(
                request.inp.case.case_id, event.party, f"inbound:{event.event_id}",
                f"{event.party}: {event.text}", "inbound_scripted_callback", self.trace, request.business_time,
            )
            self.trace.append(request.inp.case.case_id, "mock_evidence_received", {
                "business_time_utc": request.business_time.isoformat(), "event": event.model_dump(mode="json"),
                "evidence_source": "scripted mock-provider metadata accompanying this transcript; not LLM extraction",
            })
        if not events:
            await mock_call(request.inp.case.case_id, Party.PCP, "initial-request",
                            "Office: Please coordinate the requested assessment appointment and report with the patient.",
                            "inbound_scripted_request", self.trace, request.business_time)
        return events

    @activity.defn
    async def dispatch_action(self, request: ActionRequest) -> None:
        action = request.snapshot.last_action or {}
        self.trace.append(request.case_id, "coordinator_action_committed", {
            "business_time_utc": request.business_time.isoformat(), "action": action,
            "tasks": [t.model_dump(mode="json") for t in request.snapshot.tasks],
        })
        if action.get("not_sent"):
            return
        if action.get("kind") == "contact":
            party = Party(action["party"])
            delayed = request.case_id == "delayed_pt_reply" and party is Party.PATIENT and len(self.store.read(request.case_id).actions) == 1
            await mock_call(request.case_id, party, action["action_id"],
                            f"Agent: {action['message']}\n{party}: Acknowledged; a callback follows in the scenario.",
                            "outbound_coordination", self.trace, request.business_time, no_answer=delayed)
        elif action.get("kind") == "book_visit":
            await mock_call(request.case_id, Party.PCP, action["action_id"],
                            f"Agent: Book the patient-accepted {action['slot_id']}.\nOffice: That appointment is confirmed.",
                            "outbound_booking", self.trace, request.business_time)
