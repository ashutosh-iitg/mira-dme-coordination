from datetime import date

import pytest

from mira.models import CallDisposition, CallOutcome, CallPurpose, WrittenOrder
from mira.policy import ScreenDecision, assess_supplier_call, validate_order
from tests.fakes import supplier

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


def test_wrong_code_and_missing_npi_are_both_reported() -> None:
    order = VALID_ORDER.model_copy(update={"hcpcs_code": "K0003", "item_description": "Lightweight wheelchair", "prescriber_npi": None})

    problems = validate_order(order, "K0001", "Eleanor Martinez")

    assert any("K0003" in p for p in problems)
    assert any("NPI" in p for p in problems)
    assert any("manual wheelchair" in p for p in problems)


def test_stale_visit_and_unsigned_order_are_rejected() -> None:
    order = VALID_ORDER.model_copy(update={"face_to_face_date": date(2026, 1, 2), "signed": False})

    problems = validate_order(order, "K0001", "Eleanor Martinez")

    assert any("not signed" in p for p in problems)
    assert any("days before the order" in p for p in problems)
