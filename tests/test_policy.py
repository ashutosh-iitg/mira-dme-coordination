from datetime import date

import pytest
from pydantic import ValidationError

from mira.models import (
    CallDisposition,
    CallOutcome,
    CallPurpose,
    DeliveryStatus,
    FollowupResult,
    InformationRequirement,
    Supplier,
    SupplierScreenResult,
    SupplierStatus,
    SupplierVerdict,
    WrittenOrder,
)
from mira.policy import (
    FollowupDecision,
    ScreenDecision,
    assess_followup,
    assess_supplier_call,
    order_candidates,
    requirement_to_coordinate,
    validate_order,
)
from tests.fakes import followup, supplier

VALID_ORDER = WrittenOrder(
    hcpcs_code="K0001",
    item_description="Standard manual wheelchair",
    quantity=1,
    patient_name="Eleanor Martinez",
    prescriber_name="Dr. Sarah Chen",
    prescriber_npi="1234567893",
    face_to_face_date=date(2026, 11, 15),
    order_date=date(2026, 11, 23),
    signed=True,
)


def _outcome(extraction=None, disposition=CallDisposition.CONNECTED, confidence=0.9) -> CallOutcome:
    return CallOutcome(
        call_id="c1",
        purpose=CallPurpose.SUPPLIER_SCREEN,
        party_name="Acme",
        disposition=disposition,
        transcript="",
        confidence=confidence,
        supplier=extraction.result if extraction else None,
    )


@pytest.mark.parametrize(
    ("extraction", "expected", "reason_fragment"),
    [
        (supplier(True, True, True, 3), ScreenDecision.QUALIFIED, "committed"),
        (supplier(False, True, True, 3), ScreenDecision.REJECTED, "not accepting"),
        (supplier(True, False, None, None), ScreenDecision.REJECTED, "not in stock"),
        (supplier(True, True, False, 3), ScreenDecision.REJECTED, "declined"),
        (supplier(True, None, True, 3), ScreenDecision.RETRY, "in stock"),
        (supplier(True, True, True, 3, serves_area=None), ScreenDecision.RETRY, "patient's area"),
        (supplier(True, True, True, 3, serves_area=False), ScreenDecision.REJECTED, "does not deliver"),
    ],
)
def test_supplier_assessment(extraction, expected, reason_fragment) -> None:
    assessment = assess_supplier_call(_outcome(extraction), "K0001")

    assert assessment.decision is expected
    assert reason_fragment in assessment.reason


def test_unanswered_supplier_call_is_retried() -> None:
    assessment = assess_supplier_call(_outcome(disposition=CallDisposition.VOICEMAIL), "K0001")

    assert assessment.decision is ScreenDecision.RETRY


def test_low_confidence_extraction_is_retried_not_trusted() -> None:
    assessment = assess_supplier_call(_outcome(supplier(True, True, True, 3), confidence=0.3), "K0001")

    assert assessment.decision is ScreenDecision.RETRY


def test_valid_order_has_no_problems() -> None:
    assert validate_order(VALID_ORDER, "K0001", "Eleanor Martinez") == []


def test_wrong_code_is_reported_even_when_description_names_a_wheelchair() -> None:
    order = VALID_ORDER.model_copy(update={"hcpcs_code": "K0003", "item_description": "Lightweight wheelchair"})

    problems = validate_order(order, "K0001", "Eleanor Martinez")

    assert problems == ["HCPCS code is K0003, expected K0001"]


def test_npi_is_optional_when_practitioner_name_is_present() -> None:
    assert validate_order(VALID_ORDER.model_copy(update={"prescriber_npi": None}), "K0001", "Eleanor Martinez") == []

    nameless = VALID_ORDER.model_copy(update={"prescriber_npi": None, "prescriber_name": " "})
    assert validate_order(nameless, "K0001", "Eleanor Martinez") == ["treating practitioner name or NPI missing"]


def test_unsigned_or_backdated_order_is_rejected_but_old_visit_is_not_a_blanket_rule() -> None:
    unsigned_backdated = VALID_ORDER.model_copy(update={"order_date": date(2026, 11, 1), "signed": False})
    old_visit = VALID_ORDER.model_copy(update={"face_to_face_date": date(2026, 1, 2)})

    problems = validate_order(unsigned_backdated, "K0001", "Eleanor Martinez")

    assert any("not signed" in p for p in problems)
    assert any("before the visit" in p for p in problems)
    assert validate_order(old_visit, "K0001", "Eleanor Martinez") == []


def _followup_outcome(extraction, disposition=CallDisposition.CONNECTED) -> CallOutcome:
    return CallOutcome(
        call_id="f1",
        purpose=CallPurpose.SUPPLIER_FOLLOWUP,
        party_name="Acme",
        disposition=disposition,
        transcript="",
        confidence=extraction.confidence,
        followup=extraction.result,
    )


@pytest.mark.parametrize(
    ("extraction", "expected"),
    [
        (followup(DeliveryStatus.TENTATIVE, eta=4), FollowupDecision.RETRY),
        (followup(DeliveryStatus.NOT_SCHEDULED, eta=None), FollowupDecision.RETRY),
        (followup(DeliveryStatus.SCHEDULED, eta=None), FollowupDecision.SCHEDULED),
        (followup(DeliveryStatus.SCHEDULED, received=False, eta=2), FollowupDecision.RETRY),
        (followup(DeliveryStatus.DECLINED), FollowupDecision.DECLINED),
        (followup(DeliveryStatus.UNCLEAR, eta=3), FollowupDecision.RETRY),
    ],
    ids=["conditional-eta", "accepted-not-scheduled", "scheduled-no-date", "contradictory", "declined", "unclear"],
)
def test_followup_requires_explicit_scheduling(extraction, expected) -> None:
    assert assess_followup(_followup_outcome(extraction)) is expected


def test_negative_eta_is_rejected_by_the_model_boundary() -> None:
    with pytest.raises(ValidationError):
        FollowupResult(order_received=True, delivery_status=DeliveryStatus.SCHEDULED, delivery_eta_days=-1, notes="")
    with pytest.raises(ValidationError):
        SupplierScreenResult(
            accepting_new_medicare_patients=True,
            has_equipment_in_stock=True,
            serves_patient_area=True,
            delivery_eta_days=-2,
            committed_to_deliver=True,
            notes="",
        )


def _verdict(name: str, eta: int | None, miles: float) -> SupplierVerdict:
    return SupplierVerdict(
        supplier=Supplier(supplier_name=name, phone=name, address="x", zip_code="60614"),
        distance_miles=miles,
        status=SupplierStatus.QUALIFIED,
        reason="",
        attempts=1,
        delivery_eta_days=eta,
    )


def test_candidates_rank_same_day_first_and_unknown_eta_last() -> None:
    ranked = order_candidates([_verdict("unknown", None, 0.5), _verdict("four-days", 4, 1.0), _verdict("same-day", 0, 9.0)])

    assert [v.supplier.supplier_name for v in ranked] == ["same-day", "four-days", "unknown"]


@pytest.mark.parametrize("description, document_type", [
    ("Written order", "written order"),
    ("Just get us the signed order", None),
    ("Standard written order for K0001", None),
    ("SWO from the PCP", None),
])
def test_requests_for_the_written_order_are_not_coordinator_work(description: str, document_type: str | None) -> None:
    requirement = InformationRequirement(description=description, document_type=document_type, clinical_decision_required=False)

    assert requirement_to_coordinate(requirement) is None


@pytest.mark.parametrize("description, document_type", [
    ("Patient mobility assessment report", "mobility assessment report"),
    ("Proof of the patient's secondary insurance", None),
    ("Copy of the prescription history from the pharmacy", None),
])
def test_other_supplier_requests_still_go_to_the_coordinator(description: str, document_type: str | None) -> None:
    requirement = InformationRequirement(description=description, document_type=document_type, clinical_decision_required=False)

    assert requirement_to_coordinate(requirement) is requirement
