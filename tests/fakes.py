"""Keyword-driven stand-in for the LLM so workflow tests run offline and deterministically."""

from typing import Any

from mira.extraction import FollowupExtraction, PatientExtraction, PcpExtraction, SupplierExtraction
from mira.models import (
    DeliveryStatus,
    FollowupResult,
    PatientUpdateResult,
    PcpOrderResult,
    PcpOrderStatus,
    SupplierScreenResult,
)

CONFIDENT = 0.9


def supplier(
    accepting: bool | None,
    in_stock: bool | None,
    committed: bool | None,
    eta: int | None,
    serves_area: bool | None = True,
    accepts_assignment: bool | None = None,
) -> SupplierExtraction:
    return SupplierExtraction(
        result=SupplierScreenResult(
            accepting_new_medicare_patients=accepting,
            has_equipment_in_stock=in_stock,
            serves_patient_area=serves_area,
            delivery_eta_days=eta,
            committed_to_deliver=committed,
            accepts_assignment=accepts_assignment,
            notes="",
        ),
        confidence=CONFIDENT,
    )


def followup(status: DeliveryStatus, received: bool | None = True, eta: int | None = None) -> FollowupExtraction:
    return FollowupExtraction(
        result=FollowupResult(order_received=received, delivery_status=status, delivery_eta_days=eta, notes=""),
        confidence=CONFIDENT,
    )


def pcp(status: PcpOrderStatus) -> PcpExtraction:
    return PcpExtraction(
        result=PcpOrderResult(order_status=status, contact_name=None, expected_in_days=None, notes=""),
        confidence=CONFIDENT,
    )


def patient(understood: bool) -> PatientExtraction:
    return PatientExtraction(
        result=PatientUpdateResult(
            understood_next_steps=understood, understood_cost_share=understood, open_questions=[], notes=""
        ),
        confidence=CONFIDENT,
    )


RULES: list[tuple[str, Any]] = [
    ("stopped taking new Medicare", supplier(False, None, False, None)),
    ("put us down", supplier(True, True, True, 3)),
    ("we're out", supplier(True, False, None, None)),
    ("we've got plenty", supplier(True, True, True, 4, accepts_assignment=True)),
    ("on the truck", followup(DeliveryStatus.SCHEDULED, eta=4)),
    ("send it over to Dr. Chen's MA", pcp(PcpOrderStatus.IN_PROGRESS)),
    ("I don't see any order request", pcp(PcpOrderStatus.NOT_RECEIVED)),
    ("Dr. Chen signed it this morning", pcp(PcpOrderStatus.SIGNED_AND_SENT)),
    ("I'm faxing it now", pcp(PcpOrderStatus.SIGNED_AND_SENT)),
    ("I'll pass it to Dr. Chen's MA", pcp(PcpOrderStatus.IN_PROGRESS)),
    ("mobility assessment", pcp(PcpOrderStatus.NEEDS_INFO)),
    ("Twenty percent of what", patient(False)),
    ("thank you dear", patient(True)),
]


async def fake_complete(system_prompt: str, user_prompt: str, schema: type) -> Any:
    for needle, extraction in RULES:
        if needle in user_prompt and isinstance(extraction, schema):
            return extraction
    raise AssertionError(f"No fake LLM rule for {schema.__name__} in prompt: {user_prompt[-200:]!r}")
