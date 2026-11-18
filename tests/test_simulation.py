import json
from datetime import datetime

import pytest

from mira.coordinator.evaluate import SCENARIOS_PATH
from mira.coordinator.models import ActionKind, EventKind, Party, ProposedAction
from mira.extraction import FollowupExtraction, PatientExtraction, PcpExtraction, SupplierExtraction
from mira.models import DeliveryStatus, PcpOrderStatus
from mira.simulation.run import run_one
from tests.fakes import followup, patient, pcp, supplier

SCENARIOS = json.loads(SCENARIOS_PATH.read_text())


async def scripted_planner(doc):
    base = {"task_id": "followup", "reason": "Offline scripted planner for workflow wiring tests"}
    if "clinical-judgment" in doc.tasks["followup"].goal:
        return ProposedAction(**base, kind=ActionKind.REVIEW)
    accepted = [e for e in doc.events if e["kind"] in {EventKind.REPORT_ACCEPTED, EventKind.QUOTE_ACCEPTED}]
    quotes = [e for e in doc.events if e["kind"] == EventKind.QUOTE]
    if accepted:
        latest = accepted[-1]
        if latest["kind"] == EventKind.REPORT_ACCEPTED or (quotes and latest["reference_event_id"] == quotes[-1]["event_id"]):
            return ProposedAction(**base, kind=ActionKind.COMPLETE, evidence_event_id=latest["event_id"])
    consents = [e for e in doc.events if e["kind"] == EventKind.SLOT_ACCEPTED]
    if consents:
        consent = consents[-1]
        slot = next(s for s in doc.slots if s.slot_id == consent["slot_id"])
        if slot.offer_expires_at <= datetime.fromisoformat(consent["received_at"]):
            return ProposedAction(**base, kind=ActionKind.CONTACT, party=Party.PCP, message="Please provide a fresh offer")
        return ProposedAction(**base, kind=ActionKind.BOOK, slot_id=slot.slot_id, evidence_event_id=consent["event_id"])
    party = Party.PATIENT if doc.tasks["followup"].kind == "pt_followup" else Party.SUPPLIER
    if quotes and accepted:
        party = Party.PATIENT
    return ProposedAction(**base, kind=ActionKind.CONTACT, party=party, message="Please confirm the requested information")


async def scripted_extraction(system_prompt, user_prompt, schema):
    if schema is SupplierExtraction:
        return supplier(True, True, True, 2, accepts_assignment=True)
    if schema is FollowupExtraction:
        return followup(DeliveryStatus.SCHEDULED, eta=2)
    if schema is PcpExtraction:
        return pcp(PcpOrderStatus.SIGNED_AND_SENT)
    if schema is PatientExtraction:
        return patient(True)
    raise AssertionError(f"Unexpected schema {schema}")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
async def test_full_temporal_voice_coordinator_and_dme_path(scenario, tmp_path):
    folder = tmp_path / scenario["id"]
    result = await run_one(scenario, folder, scripted_planner, scripted_extraction)
    assert result["passed"]
    assert result["outcome"] == ("human_review_required" if scenario["id"] == "clinical_boundary" else "delivery_scheduled")
    events = [json.loads(line) for line in (folder / "trace.jsonl").read_text().splitlines()]
    assert any(e["kind"] == "mock_call_completed" and e.get("transcript") for e in events)
    assert all(e["actual_time_utc"] for e in events)
    histories = list((folder / "histories").glob("*.json"))
    assert len(histories) >= 3
    assert not list((folder / "histories").glob("*.missing.txt"))
    if scenario["id"] != "clinical_boundary":
        assert result["dme"]["outcome"] == "delivery_scheduled"
        assert any(e["kind"] == "dme_case_unblocked" for e in events)
        assert any(e["kind"] == "mock_call_completed" and e.get("direction") == "outbound_dme" for e in events)
    else:
        assert result["dme"] is None
        assert any(e["kind"] == "mock_human_decision" for e in events)


async def test_clinical_misroute_is_preserved_and_does_not_unblock_dme(tmp_path):
    async def wrong_planner(doc):
        return ProposedAction(task_id="followup", kind=ActionKind.CONTACT, party=Party.PATIENT,
                              message="When are you available?", reason="Intentionally wrong clinical routing")

    result = await run_one(SCENARIOS[-1], tmp_path / "misroute", wrong_planner, scripted_extraction)
    assert result["passed"] is False
    assert result["outcome"] == "human_review_required"
    assert result["dme"] is None
    assert result["stage_checks"][0]["passed"] is False
