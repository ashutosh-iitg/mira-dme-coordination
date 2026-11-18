import json
import logging
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from mira.config import RUNTIME_DIR
from mira.data import load_suppliers
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

ESCALATIONS_PATH = RUNTIME_DIR / "escalations.json"


class CaseActivities:
    def __init__(self, events: EventLog, fax_inbox: MockFaxInbox, escalations_path: Path = ESCALATIONS_PATH) -> None:
        self._events = events
        self._fax_inbox = fax_inbox
        self._escalations_path = escalations_path

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
        existing = self._read_escalations()
        if any(e.get("escalation_id") == escalation.escalation_id for e in existing):
            return escalation.escalation_id
        self._escalations_path.parent.mkdir(parents=True, exist_ok=True)
        updated = [*existing, {**escalation.model_dump(mode="json"), "status": "open"}]
        self._escalations_path.write_text(json.dumps(updated, indent=2), encoding="utf-8")
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
        updated = [
            {**e, **resolved} if e.get("escalation_id") == resolution.escalation_id else e for e in self._read_escalations()
        ]
        self._escalations_path.parent.mkdir(parents=True, exist_ok=True)
        self._escalations_path.write_text(json.dumps(updated, indent=2), encoding="utf-8")
        self._events.append(
            resolution.case_id, "escalation_resolved", {"escalation_id": resolution.escalation_id, **resolved}
        )

    @activity.defn
    async def get_escalation(self, escalation_id: str) -> Escalation:
        for record in self._read_escalations():
            if record.get("escalation_id") == escalation_id:
                return Escalation.model_validate({k: record[k] for k in Escalation.model_fields})
        raise ApplicationError(f"Escalation {escalation_id} not found", type="EscalationNotFound", non_retryable=True)

    @activity.defn
    async def record_event(self, event: CaseEvent) -> None:
        self._events.append(event.case_id, event.kind, event.details)

    def _read_escalations(self) -> list[dict]:
        if not self._escalations_path.exists():
            return []
        try:
            return json.loads(self._escalations_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Escalations file {self._escalations_path} is corrupt: {exc}") from exc
