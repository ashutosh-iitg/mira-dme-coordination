"""Deterministic business rules. AI is deliberately kept out of these decisions."""

from dataclasses import dataclass
from enum import StrEnum

from mira.models import CallDisposition, CallOutcome, WrittenOrder

MIN_EXTRACTION_CONFIDENCE = 0.6
MAX_SERVICE_RADIUS_MILES = 15.0
MAX_VISIT_TO_ORDER_DAYS = 180
REQUIRED_DESCRIPTION_TERMS = ("manual", "wheelchair")


class ScreenDecision(StrEnum):
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    RETRY = "retry"


@dataclass(frozen=True)
class SupplierAssessment:
    decision: ScreenDecision
    reason: str


def assess_supplier_call(outcome: CallOutcome, equipment_code: str) -> SupplierAssessment:
    if outcome.disposition is not CallDisposition.CONNECTED:
        return SupplierAssessment(ScreenDecision.RETRY, f"call not answered ({outcome.disposition})")

    screen = outcome.supplier
    if screen is None or outcome.confidence < MIN_EXTRACTION_CONFIDENCE:
        return SupplierAssessment(ScreenDecision.RETRY, f"call unclear (confidence {outcome.confidence:.2f})")

    if screen.accepting_new_medicare_patients is False:
        return SupplierAssessment(ScreenDecision.REJECTED, "not accepting new Medicare patients")
    if screen.has_equipment_in_stock is False:
        return SupplierAssessment(ScreenDecision.REJECTED, f"{equipment_code} not in stock")
    if screen.serves_patient_area is False:
        return SupplierAssessment(ScreenDecision.REJECTED, "does not deliver to patient's area")
    if screen.committed_to_deliver is False:
        return SupplierAssessment(ScreenDecision.REJECTED, "declined the case")

    unanswered = [
        label
        for label, value in (
            ("accepting new patients", screen.accepting_new_medicare_patients),
            ("in stock", screen.has_equipment_in_stock),
            ("committed", screen.committed_to_deliver),
        )
        if value is None
    ]
    if unanswered:
        return SupplierAssessment(ScreenDecision.RETRY, f"unconfirmed: {', '.join(unanswered)}")
    return SupplierAssessment(ScreenDecision.QUALIFIED, "enrolled, accepting, in stock, committed")


class FollowupDecision(StrEnum):
    SCHEDULED = "scheduled"
    DECLINED = "declined"
    RETRY = "retry"


def assess_followup(outcome: CallOutcome) -> FollowupDecision:
    """After handoff, only an explicit commitment or a delivery date counts as scheduled."""
    screen = outcome.supplier
    if outcome.disposition is not CallDisposition.CONNECTED or screen is None:
        return FollowupDecision.RETRY
    if outcome.confidence < MIN_EXTRACTION_CONFIDENCE:
        return FollowupDecision.RETRY
    if screen.committed_to_deliver is False or screen.has_equipment_in_stock is False:
        return FollowupDecision.DECLINED
    if screen.committed_to_deliver or screen.delivery_eta_days is not None:
        return FollowupDecision.SCHEDULED
    return FollowupDecision.RETRY


def claim_code_problems(order: WrittenOrder, ordered_code: str) -> list[str]:
    """The claim is billed on the order's code; it must match what the patient actually needs."""
    if order.hcpcs_code.strip().upper() != ordered_code.upper():
        return [f"order code {order.hcpcs_code} does not match the item being delivered ({ordered_code})"]
    return []


def within_service_radius(distance_miles: float | None) -> bool:
    return distance_miles is None or distance_miles <= MAX_SERVICE_RADIUS_MILES


def validate_order(order: WrittenOrder, expected_code: str, patient_name: str) -> list[str]:
    """Checks Standard Written Order elements and code match before a claim can be billed."""
    problems: list[str] = []
    if order.hcpcs_code.strip().upper() != expected_code.upper():
        problems.append(f"HCPCS code is {order.hcpcs_code}, expected {expected_code}")
    description = order.item_description.lower()
    if not all(term in description for term in REQUIRED_DESCRIPTION_TERMS):
        problems.append(f"item description {order.item_description!r} does not describe a manual wheelchair")
    if order.quantity != 1:
        problems.append(f"quantity is {order.quantity}, expected 1")
    if order.patient_name.strip().casefold() != patient_name.strip().casefold():
        problems.append(f"patient name {order.patient_name!r} does not match {patient_name!r}")
    if not order.prescriber_npi:
        problems.append("prescriber NPI missing")
    if not order.signed:
        problems.append("order is not signed")
    visit_gap_days = (order.order_date - order.face_to_face_date).days
    if visit_gap_days < 0:
        problems.append("order is dated before the face-to-face visit")
    elif visit_gap_days > MAX_VISIT_TO_ORDER_DAYS:
        problems.append(f"face-to-face visit is {visit_gap_days} days before the order (max {MAX_VISIT_TO_ORDER_DAYS})")
    return problems
