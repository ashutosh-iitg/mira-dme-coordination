from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Patient(Frozen):
    name: str
    age: int
    phone: str
    home_zip: str
    coverage: str
    has_supplemental: bool


class Equipment(Frozen):
    description: str
    hcpcs_code: str


class Pcp(Frozen):
    name: str
    practice: str
    city: str
    phone: str


class Case(Frozen):
    case_id: str
    patient: Patient
    equipment: Equipment
    pcp: Pcp
    pcp_visit_days_ago: int
    order_status: str
    assumptions: list[str] = Field(default_factory=list)


class Supplier(Frozen):
    supplier_name: str
    phone: str
    address: str
    zip_code: str


class CallPurpose(StrEnum):
    SUPPLIER_SCREEN = "supplier_screen"
    SUPPLIER_FOLLOWUP = "supplier_followup"
    PCP_ORDER_REQUEST = "pcp_order_request"
    PCP_ORDER_NUDGE = "pcp_order_nudge"
    PATIENT_UPDATE = "patient_update"


class CallDisposition(StrEnum):
    CONNECTED = "connected"
    NO_ANSWER = "no_answer"
    VOICEMAIL = "voicemail"


class CallRequest(Frozen):
    case_id: str
    call_id: str
    purpose: CallPurpose
    party_name: str
    phone: str
    attempt: int = 1
    talking_points: list[str] = Field(default_factory=list)


class SupplierScreenResult(Frozen):
    accepting_new_medicare_patients: bool | None
    has_equipment_in_stock: bool | None
    serves_patient_area: bool | None
    delivery_eta_days: int | None = Field(default=None, ge=0)
    committed_to_deliver: bool | None
    accepts_assignment: bool | None = None
    notes: str


class DeliveryStatus(StrEnum):
    SCHEDULED = "scheduled"
    TENTATIVE = "tentative"
    NOT_SCHEDULED = "not_scheduled"
    DECLINED = "declined"
    UNCLEAR = "unclear"


class FollowupResult(Frozen):
    """What a supplier says after receiving the order; kept separate from screening on purpose."""

    order_received: bool | None
    delivery_status: DeliveryStatus
    delivery_eta_days: int | None = Field(default=None, ge=0)
    notes: str


class PcpOrderStatus(StrEnum):
    NOT_RECEIVED = "not_received"
    IN_PROGRESS = "in_progress"
    SIGNED_AND_SENT = "signed_and_sent"
    NEEDS_INFO = "needs_info"
    UNCLEAR = "unclear"


class PcpOrderResult(Frozen):
    order_status: PcpOrderStatus
    contact_name: str | None
    expected_in_days: int | None
    notes: str


class PatientUpdateResult(Frozen):
    understood_next_steps: bool | None
    understood_cost_share: bool | None
    open_questions: list[str]
    notes: str


class CallOutcome(Frozen):
    call_id: str
    purpose: CallPurpose
    party_name: str
    disposition: CallDisposition
    transcript: str
    confidence: float = Field(ge=0.0, le=1.0)
    supplier: SupplierScreenResult | None = None
    followup: FollowupResult | None = None
    pcp: PcpOrderResult | None = None
    patient: PatientUpdateResult | None = None


class WrittenOrder(Frozen):
    hcpcs_code: str
    item_description: str
    quantity: int
    patient_name: str
    prescriber_name: str
    prescriber_npi: str | None
    face_to_face_date: date
    order_date: date
    signed: bool


class Escalation(Frozen):
    escalation_id: str
    case_id: str
    reason: str
    context: dict[str, str] = Field(default_factory=dict)


class CaseEvent(Frozen):
    case_id: str
    kind: str
    details: dict[str, str] = Field(default_factory=dict)


class RankRequest(Frozen):
    patient_zip: str
    excluded_phones: list[str] = Field(default_factory=list)


class RankedSupplier(Frozen):
    supplier: Supplier
    distance_miles: float | None


class OrderRequestFax(Frozen):
    case_id: str
    pcp: Pcp
    patient_name: str
    equipment: Equipment
    reason: str


class FetchOrderRequest(Frozen):
    case_id: str
    fetch_attempt: int


class SupplierStatus(StrEnum):
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    UNREACHABLE = "unreachable"


class SupplierVerdict(Frozen):
    supplier: Supplier
    distance_miles: float | None
    status: SupplierStatus
    reason: str
    attempts: int
    delivery_eta_days: int | None = None
    accepts_assignment: bool | None = None
    call_id: str | None = None


class SourcingInput(Frozen):
    case_id: str
    equipment: Equipment
    patient_zip: str
    seconds_per_day: float
    excluded_phones: list[str] = Field(default_factory=list)
    wanted_candidates: int = 2
    max_attempts_per_supplier: int = 3
    callback_delay_days: float = 1.0


class SourcingResult(Frozen):
    qualified: list[SupplierVerdict]
    others: list[SupplierVerdict]
    escalation_id: str | None = None


class OrderInput(Frozen):
    case_id: str
    patient_name: str
    equipment: Equipment
    pcp: Pcp
    seconds_per_day: float
    nudge_interval_days: float = 2.0
    max_nudges: int = 4
    sla_days: float = 10.0
    max_call_attempts: int = 3
    no_answer_retry_days: float = 0.5


class OrderOutcomeStatus(StrEnum):
    VALID = "valid"
    ESCALATED = "escalated"


class OrderResult(Frozen):
    status: OrderOutcomeStatus
    order: WrittenOrder | None
    nudges: int
    problems_seen: list[str]
    escalation_id: str | None = None


class CaseInput(Frozen):
    case: Case
    seconds_per_day: float


class HumanAction(StrEnum):
    RETRY = "retry"
    CLOSE = "close"


class HumanDecision(Frozen):
    escalation_id: str
    action: HumanAction
    note: str = ""


class ResolveEscalation(Frozen):
    escalation_id: str
    case_id: str
    action: HumanAction
    note: str


class SupplierHandoff(Frozen):
    case_id: str
    supplier: Supplier
    patient_name: str
    patient_zip: str
    order: WrittenOrder


class FulfillmentInput(Frozen):
    case_id: str
    patient_name: str
    patient_zip: str
    equipment: Equipment
    order: WrittenOrder
    candidates: list[SupplierVerdict]
    seconds_per_day: float
    followup_after_days: float = 1.0
    max_followup_calls: int = 3
    followup_retry_days: float = 0.5


class HandoffResult(StrEnum):
    SCHEDULED = "scheduled"
    DECLINED = "declined"
    UNCONFIRMED = "unconfirmed"
    SILENT = "silent"


class FulfillmentAttempt(Frozen):
    supplier_name: str
    phone: str
    result: HandoffResult
    followup_calls: int
    delivery_eta_days: int | None = None


class FulfillmentResult(Frozen):
    scheduled_with: SupplierVerdict | None
    delivery_eta_days: int | None
    attempts: list[FulfillmentAttempt]
    escalation_id: str | None = None


class PatientContact(Frozen):
    milestone: str
    reached: bool
    understood_cost_share: bool | None = None
    open_questions: list[str] = Field(default_factory=list)
    escalation_id: str | None = None


class CaseOutcome(StrEnum):
    DELIVERY_SCHEDULED = "delivery_scheduled"
    CLOSED_BY_HUMAN = "closed_by_human"


class PendingReview(Frozen):
    escalation_id: str
    review_workflow_id: str
    reason: str
    context: dict[str, str] = Field(default_factory=dict)


class HumanReviewInput(Frozen):
    case_id: str
    escalation: Escalation


class CaseStatus(Frozen):
    stage: str
    pending_reviews: list[PendingReview]
    supplier: str | None
    delivery_eta_days: int | None


class CaseSummary(Frozen):
    case_id: str
    outcome: CaseOutcome
    sourcing: SourcingResult | None
    order: OrderResult | None
    fulfillment: FulfillmentResult | None
    patient_contacts: list[PatientContact]
    escalations: list[str]
    closing_note: str = ""
