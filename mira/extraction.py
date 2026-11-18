from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, Field

from mira.llm import complete_structured
from mira.models import (
    CallDisposition,
    CallOutcome,
    CallPurpose,
    CallRequest,
    CoordinationObservation,
    FollowupResult,
    Frozen,
    PatientUpdateResult,
    PcpOrderResult,
    SupplierScreenResult,
)
from mira.voice.agent import RawCall

T = TypeVar("T", bound=BaseModel)
StructuredCompleter = Callable[[str, str, type[T]], Awaitable[T]]

SYSTEM_PROMPT = """You read transcripts of phone calls placed by an automated care-coordination agent \
on behalf of a Medicare patient who needs durable medical equipment.
Extract only what the other party actually said. If something was not stated or is ambiguous, use null — never guess.
`confidence` is your 0-1 confidence that the extracted fields faithfully reflect the call.
Reply with JSON matching the schema and nothing else."""


class SupplierExtraction(Frozen):
    result: SupplierScreenResult
    confidence: float = Field(ge=0.0, le=1.0)


class FollowupExtraction(Frozen):
    result: FollowupResult
    confidence: float = Field(ge=0.0, le=1.0)


class PcpExtraction(Frozen):
    result: PcpOrderResult
    confidence: float = Field(ge=0.0, le=1.0)


class PatientExtraction(Frozen):
    result: PatientUpdateResult
    confidence: float = Field(ge=0.0, le=1.0)


class CoordinationExtraction(Frozen):
    result: CoordinationObservation
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True)
class PurposeSpec:
    instructions: str
    schema: type[SupplierExtraction | FollowupExtraction | PcpExtraction | PatientExtraction | CoordinationExtraction]
    outcome_field: str


SUPPLIER_SPEC = PurposeSpec(
    instructions=(
        "The agent called a DME supplier to ask whether they take new Original Medicare patients, "
        "have a standard manual wheelchair (HCPCS K0001) in stock, deliver to the patient's area, and how soon. "
        "committed_to_deliver is true only if the supplier explicitly agreed to take this patient. "
        "delivery_eta_days is a whole number of days; if they give a range (e.g. 'two, three days'), use the upper bound. "
        "accepts_assignment is true only if they say they accept Medicare assignment (bill Medicare's approved amount). "
        "If they request information or a document from the patient/PCP before proceeding, populate information_request "
        "(but NOT for the signed written order itself: we always send that at handoff, so 'just get us the order' is no request) "
        "with the exact requirement, document_type if applicable, and whether it needs a clinical decision. "
        "Set clinical_decision_required false only for clearly administrative requests, true for clinical judgment, "
        "and null if unclear. Do not invent requirements."
    ),
    schema=SupplierExtraction,
    outcome_field="supplier",
)

FOLLOWUP_SPEC = PurposeSpec(
    instructions=(
        "The agent called a DME supplier that was already sent the patient's signed order, to confirm delivery. "
        "delivery_status: 'scheduled' ONLY if the supplier says delivery is actually booked or on the schedule "
        "(a day, a truck, an appointment); 'tentative' if they only give typical timing or conditions "
        "(e.g. 'usually four days after eligibility clears'); 'not_scheduled' if they acknowledge the case but have not "
        "booked it; 'declined' if they will not supply it; 'unclear' otherwise. "
        "order_received is whether they confirm having the signed order. delivery_eta_days may be null even when scheduled. "
        "If they request patient/PCP information or a document before proceeding, populate information_request "
        "(not for the signed written order itself, which they already have or will get at handoff). "
        "An unresolved requirement is not confirmed scheduling. Set its clinical_decision_required false only when "
        "the request is clearly administrative, true for clinical judgment, and null if its scope is unclear."
    ),
    schema=FollowupExtraction,
    outcome_field="followup",
)

PCP_SPEC = PurposeSpec(
    instructions=(
        "The agent called the primary care doctor's office about the written order for the patient's wheelchair. "
        "Classify where the order stands. not_received means the office says they have no request; "
        "needs_info means they asked for something before they can proceed. When needs_info, populate information_request "
        "with exactly what they need, document_type if they want a document, and clinical_decision_required: false only "
        "when someone else already has it and it just needs to reach the office (e.g. a copy of an existing report), "
        "true when it needs a new clinical evaluation, visit, or judgment, null if unclear. Do not invent requirements."
    ),
    schema=PcpExtraction,
    outcome_field="pcp",
)

PATIENT_SPEC = PurposeSpec(
    instructions=(
        "The agent called the patient with a status update, next steps, and her expected ~20% cost share. "
        "Record whether she appeared to understand, and any questions she raised that a human should follow up on. "
        "If her questions can be answered by the supplier or the doctor's office (cost estimate, billing, delivery, setup), "
        "also put them in information_request as one combined request with clinical_decision_required false; "
        "use true for medical questions and null if unclear. Leave it null if she has no questions."
    ),
    schema=PatientExtraction,
    outcome_field="patient",
)

PURPOSE_SPECS: dict[CallPurpose, PurposeSpec] = {
    CallPurpose.SUPPLIER_SCREEN: SUPPLIER_SPEC,
    CallPurpose.SUPPLIER_FOLLOWUP: FOLLOWUP_SPEC,
    CallPurpose.PCP_ORDER_REQUEST: PCP_SPEC,
    CallPurpose.PCP_ORDER_NUDGE: PCP_SPEC,
    CallPurpose.PATIENT_UPDATE: PATIENT_SPEC,
    CallPurpose.COORDINATION: PurposeSpec(
        instructions=(
            "Extract the called party's response to the specific administrative requirement. "
            "Keep document references opaque; never invent document contents or claim clinical verification. "
            "The requester (who needs it) is named in the talking points; the called party may be the requester or a source. "
            "sharing_authorized is true only for explicit patient permission to share the specific reference or information "
            "with the requester. needs_provider means the called party cannot supply it and points us to someone else. "
            "confirmed_reference is the exact reference the patient confirms and permits sharing; use the reference "
            "from the talking points only if the patient's response actually confirms it. "
            "requester_accepted is true ONLY when the called party is the requester and explicitly accepts the forwarded "
            "reference/information (e.g. 'that answers my question', 'that is what we needed', 'we accept it'); then "
            "accepted_reference is the forwarded reference ID from the talking points, even if they don't say the ID aloud. "
            "false if they say it does not satisfy them; null if they don't say. "
            "Receipt alone is not acceptance. information is the substance a source actually stated, in their words. "
            "clinical_decision_required is true for treatment, prescription changes or clinical interpretation."
        ), schema=CoordinationExtraction, outcome_field="coordination",
    ),
}


async def interpret_call(
    request: CallRequest,
    raw: RawCall,
    complete: StructuredCompleter = complete_structured,
) -> CallOutcome:
    base = {
        "call_id": request.call_id,
        "purpose": request.purpose,
        "party_name": request.party_name,
        "disposition": raw.disposition,
        "transcript": raw.transcript,
    }
    if raw.disposition is not CallDisposition.CONNECTED:
        return CallOutcome(**base, confidence=1.0)

    spec = PURPOSE_SPECS[request.purpose]
    user_prompt = (f"{spec.instructions}\n\nCalled party: {request.party_name}; role: {request.coordination_role}\n"
                   f"Requirement: {request.requirement_id}\nTalking points: {request.talking_points}\n\nTranscript:\n{raw.transcript}")
    extraction = await complete(SYSTEM_PROMPT, user_prompt, spec.schema)
    return CallOutcome(**base, confidence=extraction.confidence, **{spec.outcome_field: extraction.result})
