from typing import Any

import pytest
from tests.conftest import LINCOLN_PARK_PHONE, make_request

from mira.extraction import PcpExtraction, SupplierExtraction, interpret_call
from mira.models import CallDisposition, CallPurpose, PcpOrderResult, PcpOrderStatus, SupplierScreenResult
from mira.voice.agent import RawCall

SUPPLIER_YES = SupplierScreenResult(
    accepting_new_medicare_patients=True,
    has_equipment_in_stock=True,
    serves_patient_area=True,
    delivery_eta_days=3,
    committed_to_deliver=True,
    notes="will deliver",
)


class FakeCompleter:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[tuple[str, str, type]] = []

    async def __call__(self, system: str, user: str, schema: type) -> Any:
        self.calls.append((system, user, schema))
        return self.response


async def test_unanswered_call_skips_llm() -> None:
    completer = FakeCompleter(response=None)
    request = make_request(LINCOLN_PARK_PHONE, CallPurpose.SUPPLIER_SCREEN)

    outcome = await interpret_call(request, RawCall(disposition=CallDisposition.NO_ANSWER), completer)

    assert completer.calls == []
    assert outcome.disposition is CallDisposition.NO_ANSWER
    assert outcome.supplier is None


async def test_connected_supplier_call_uses_supplier_schema() -> None:
    completer = FakeCompleter(SupplierExtraction(result=SUPPLIER_YES, confidence=0.9))
    request = make_request(LINCOLN_PARK_PHONE, CallPurpose.SUPPLIER_SCREEN, party="Lincoln Park DME")
    raw = RawCall(disposition=CallDisposition.CONNECTED, transcript="Supplier: yes")

    outcome = await interpret_call(request, raw, completer)

    _, user_prompt, schema = completer.calls[0]
    assert schema is SupplierExtraction
    assert "Supplier: yes" in user_prompt
    assert outcome.supplier == SUPPLIER_YES
    assert outcome.confidence == pytest.approx(0.9)


async def test_pcp_nudge_routes_to_pcp_result() -> None:
    pcp_result = PcpOrderResult(order_status=PcpOrderStatus.NOT_RECEIVED, contact_name=None, expected_in_days=None, notes="")
    completer = FakeCompleter(PcpExtraction(result=pcp_result, confidence=0.8))
    request = make_request("(312) 555-0198", CallPurpose.PCP_ORDER_NUDGE)
    raw = RawCall(disposition=CallDisposition.CONNECTED, transcript="Office: never got it")

    outcome = await interpret_call(request, raw, completer)

    assert outcome.pcp == pcp_result
    assert outcome.supplier is None
