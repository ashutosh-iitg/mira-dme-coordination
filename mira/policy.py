"""Deterministic business rules. AI is deliberately kept out of these decisions."""

from dataclasses import dataclass
from enum import StrEnum

from mira.models import CallDisposition, CallOutcome, DeliveryStatus, SupplierVerdict, WrittenOrder

MIN_EXTRACTION_CONFIDENCE = 0.6
REQUIRED_DESCRIPTION_TERM = "wheelchair"


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
            ("delivers to patient's area", screen.serves_patient_area),
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
    """After handoff, only explicit scheduling evidence counts; typical timing or a bare 'yes' does not."""
    followup = outcome.followup
    if outcome.disposition is not CallDisposition.CONNECTED or followup is None:
        return FollowupDecision.RETRY
    if outcome.confidence < MIN_EXTRACTION_CONFIDENCE:
        return FollowupDecision.RETRY
    if followup.delivery_status is DeliveryStatus.DECLINED:
        return FollowupDecision.DECLINED
    if followup.delivery_status is DeliveryStatus.SCHEDULED and followup.order_received is not False:
        return FollowupDecision.SCHEDULED
    return FollowupDecision.RETRY


def order_candidates(qualified: list[SupplierVerdict]) -> list[SupplierVerdict]:
    """Primary first: soonest known delivery (0 = same day), then closest; unknown ETAs go last."""
    return sorted(
        qualified,
        key=lambda v: (v.delivery_eta_days is None, v.delivery_eta_days or 0, v.distance_miles or 0.0),
    )


def claim_code_problems(order: WrittenOrder, ordered_code: str) -> list[str]:
    """The claim is billed on the order's code; it must match what the patient actually needs."""
    if order.hcpcs_code.strip().upper() != ordered_code.upper():
        return [f"order code {order.hcpcs_code} does not match the item being delivered ({ordered_code})"]
    return []


def validate_order(order: WrittenOrder, expected_code: str, patient_name: str) -> list[str]:
    """Case-specific consistency checks on the written order before it goes to a supplier.

    Mirrors the CMS standard written order elements (beneficiary name, order date, item description,
    quantity, practitioner name or NPI, signature) plus a code match against what this patient needs.
    Passing is NOT a coverage determination: medical necessity documentation stays with the PCP and supplier.
    """
    problems: list[str] = []
    if order.hcpcs_code.strip().upper() != expected_code.upper():
        problems.append(f"HCPCS code is {order.hcpcs_code}, expected {expected_code}")
    if REQUIRED_DESCRIPTION_TERM not in order.item_description.lower():
        problems.append(f"item description {order.item_description!r} does not describe a wheelchair")
    if order.quantity != 1:
        problems.append(f"quantity is {order.quantity}, expected 1")
    if order.patient_name.strip().casefold() != patient_name.strip().casefold():
        problems.append(f"patient name {order.patient_name!r} does not match {patient_name!r}")
    if not order.prescriber_name.strip() and not order.prescriber_npi:
        problems.append("treating practitioner name or NPI missing")
    if not order.signed:
        problems.append("order is not signed")
    if order.order_date < order.face_to_face_date:
        problems.append("order is dated before the visit it is based on")
    return problems
