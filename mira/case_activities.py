import logging

from temporalio import activity
from temporalio.exceptions import ApplicationError

from mira.data import load_suppliers
from mira.escalation_store import EscalationStore
from mira.events import EventLog
from mira.mock_fax import MockFaxInbox
from mira.models import (
    CaseEvent,
    Escalation,
    FetchOrderRequest,
    OrderRequestFax,
    RankedSupplier,
    RankRequest,
    ResolveEscalation,
    SupplierHandoff,
    WrittenOrder,
)
from mira.ranking import load_zip_centroids, rank_by_distance

logger = logging.getLogger(__name__)

class CaseActivities:
    def __init__(self, events: EventLog, fax_inbox: MockFaxInbox, store: EscalationStore | None = None) -> None:
        self._events = events
        self._fax_inbox = fax_inbox
        self._store = store or EscalationStore()

    @activity.defn
    async def rank_suppliers(self, request: RankRequest) -> list[RankedSupplier]:
        return rank_by_distance(
            load_suppliers(), request.patient_zip, load_zip_centroids(), frozenset(request.excluded_phones)
        )

    @activity.defn
    async def send_order_request(self, fax: OrderRequestFax) -> None:
        """Mock outbound fax of the order request to the PCP office."""
        self._events.append(
            fax.case_id,
            "order_request_faxed",
            {"to": fax.pcp.practice, "patient": fax.patient_name, "item": fax.equipment.hcpcs_code, "reason": fax.reason},
        )

    @activity.defn
    async def fetch_written_order(self, request: FetchOrderRequest) -> WrittenOrder | None:
        order = self._fax_inbox.fetch(request.fetch_attempt)
        self._events.append(
            request.case_id,
            "fax_inbox_checked",
            {"fetch_attempt": request.fetch_attempt, "found": order is not None},
        )
        return order

    @activity.defn
    async def escalate(self, escalation: Escalation) -> str:
        """Idempotent on escalation_id so activity retries never duplicate a human task."""
        if self._store.add(escalation):
            self._events.append(escalation.case_id, "escalated", {"escalation_id": escalation.escalation_id, "reason": escalation.reason})
            logger.warning("Escalated %s: %s", escalation.escalation_id, escalation.reason)
        return escalation.escalation_id

    @activity.defn
    async def send_order_to_supplier(self, handoff: SupplierHandoff) -> None:
        """Mock fax of the signed order + patient details to the chosen supplier."""
        self._events.append(
            handoff.case_id,
            "order_sent_to_supplier",
            {"supplier": handoff.supplier.supplier_name, "code": handoff.order.hcpcs_code, "patient_zip": handoff.patient_zip},
        )

    @activity.defn
    async def resolve_escalation(self, resolution: ResolveEscalation) -> None:
        resolved = {"status": "resolved", "action": resolution.action.value, "note": resolution.note}
        self._store.update(resolution.escalation_id, resolved)
        self._events.append(resolution.case_id, "escalation_resolved", {"escalation_id": resolution.escalation_id, **resolved})

    @activity.defn
    async def get_escalation(self, escalation_id: str) -> Escalation:
        escalation = self._store.get_escalation(escalation_id)
        if escalation is None:
            raise ApplicationError(f"Escalation {escalation_id} not found", type="EscalationNotFound", non_retryable=True)
        return escalation

    @activity.defn
    async def record_event(self, event: CaseEvent) -> None:
        self._events.append(event.case_id, event.kind, event.details)
