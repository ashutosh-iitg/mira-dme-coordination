from datetime import datetime
from enum import StrEnum

from pydantic import Field

from mira.models import Frozen


class TaskKind(StrEnum):
    PT = "pt_followup"
    COST = "cost_clarification"


class TaskStatus(StrEnum):
    READY = "ready"
    WAITING = "waiting_external"
    REVIEW = "awaiting_review"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class Party(StrEnum):
    PATIENT = "patient"
    PCP = "pcp"
    SUPPLIER = "supplier"
    HUMAN = "human"


class EventKind(StrEnum):
    MESSAGE = "message"
    SLOT_OFFERED = "slot_offered"
    SLOT_ACCEPTED = "slot_accepted"
    VISIT_COMPLETED = "visit_completed"
    REPORT_ACCEPTED = "report_accepted"
    QUOTE = "quote"
    QUOTE_ACCEPTED = "quote_accepted"
    RESUME = "human_resume"
    CANCEL = "human_cancel"


class FollowupTask(Frozen):
    task_id: str = Field(min_length=1, max_length=150)
    kind: TaskKind
    goal: str = Field(min_length=1, max_length=3000)
    source_escalation_id: str | None = None
    status: TaskStatus = TaskStatus.READY
    waiting_for: Party | None = None
    next_check_at: datetime | None = None
    contacts: int = 0
    booking_reference: str | None = None
    booked_slot_id: str | None = None
    review_reason: str = ""


class IncomingEvent(Frozen):
    event_id: str = Field(min_length=1, max_length=150)
    task_id: str
    party: Party
    kind: EventKind = EventKind.MESSAGE
    text: str = Field(min_length=1, max_length=5000)
    slot_id: str | None = None
    reference_event_id: str | None = None
    amount_cents: int | None = Field(default=None, ge=0)
    accepts_assignment: bool | None = None
    slot_starts_at: datetime | None = None
    offer_expires_at: datetime | None = None


class AppointmentSlot(Frozen):
    slot_id: str
    starts_at: datetime
    offer_expires_at: datetime


class ActionKind(StrEnum):
    CONTACT = "contact"
    BOOK = "book_visit"
    COMPLETE = "complete"
    REVIEW = "request_human_review"
    WAIT = "wait"


class ProposedAction(Frozen):
    task_id: str
    kind: ActionKind
    reason: str = Field(min_length=1, max_length=1500)
    party: Party | None = None
    message: str = Field(default="", max_length=2000)
    slot_id: str | None = None
    evidence_event_id: str | None = None


class CaseDocument(Frozen):
    schema_version: int = 1
    case_id: str
    patient_name: str
    version: int = 0
    closed: bool = False
    tasks: dict[str, FollowupTask] = Field(default_factory=dict)
    slots: list[AppointmentSlot] = Field(default_factory=list)
    events: list[dict] = Field(default_factory=list)
    rejected_events: list[dict] = Field(default_factory=list)
    actions: list[dict] = Field(default_factory=list)
    turns: dict[str, dict] = Field(default_factory=dict)


class CoordinatorInput(Frozen):
    case_id: str
    patient_name: str
    tasks: list[FollowupTask]
    reminder_seconds: float = Field(default=86400, gt=0, allow_inf_nan=False)
    initial_events: list[IncomingEvent] = Field(default_factory=list)
    observer_workflow_id: str | None = None
    stop_when_settled: bool = False


class Snapshot(Frozen):
    version: int
    closed: bool
    ready: bool
    next_check_at: datetime | None
    tasks: list[FollowupTask]
    last_action: dict | None = None


class SyncRequest(Frozen):
    inp: CoordinatorInput
    now: datetime
    events: list[IncomingEvent] = Field(default_factory=list)
    tasks: list[FollowupTask] = Field(default_factory=list)
    close: bool = False


class TurnRequest(Frozen):
    case_id: str
    turn_id: str
    now: datetime
    reminder_seconds: float


class Decision(Frozen):
    request: TurnRequest
    expected_version: int
    action: ProposedAction
