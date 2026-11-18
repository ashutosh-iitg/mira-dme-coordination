import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from mira.activities import CallActivities
from mira.case_activities import CaseActivities
from mira.config import CASE_TASK_QUEUE, SCENARIOS_DIR, VOICE_CALL_TASK_QUEUE
from mira.data import load_case
from mira.escalation_store import EscalationStore
from mira.events import EventLog
from mira.extraction import StructuredCompleter
from mira.llm import LlmError
from mira.mock_fax import MockFaxInbox
from mira.models import (
    CallDisposition,
    CallPurpose,
    CaseInput,
    CaseOutcome,
    HandoffResult,
    HumanAction,
    OrderInput,
    OrderOutcomeStatus,
)
from mira.reviews import ReviewNotFound, complete_callback, send_decision
from mira.voice.agent import MockVoiceAgent, RawCall, Scenario, ScriptedParty, load_scenario
from mira.worker import WORKFLOWS, case_activity_methods
from mira.workflows.case import CaseWorkflow
from mira.workflows.human_review import HumanReviewWorkflow, review_workflow_id
from mira.workflows.pcp_order import PcpOrderWorkflow
from tests.fakes import fake_complete

SECONDS_PER_DAY = 60.0
MAX_DAYS_TO_WAIT_FOR_REVIEW = 60


@pytest.fixture(scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as environment:
        yield environment


@asynccontextmanager
async def running_workers(
    client: Client, scenario: Scenario, runtime: Path, complete: StructuredCompleter = fake_complete
) -> AsyncIterator[Path]:
    events = EventLog(runtime / "events.jsonl")
    escalations = runtime / "escalations.json"
    calls = CallActivities(MockVoiceAgent(scenario), events, complete=complete)
    case = CaseActivities(events, MockFaxInbox(scenario), store=EscalationStore(escalations))
    async with (
        Worker(client, task_queue=CASE_TASK_QUEUE, workflows=WORKFLOWS, activities=case_activity_methods(case)),
        Worker(client, task_queue=VOICE_CALL_TASK_QUEUE, activities=[calls.place_call]),
    ):
        yield escalations


async def _start_case(client: Client) -> WorkflowHandle:
    return await client.start_workflow(
        CaseWorkflow.run,
        CaseInput(case=load_case(), seconds_per_day=SECONDS_PER_DAY),
        id=f"test-case-{uuid.uuid4()}",
        task_queue=CASE_TASK_QUEUE,
    )


async def _decide_once_waiting(env: WorkflowEnvironment, escalation_id: str, action: HumanAction, note: str) -> None:
    """Advance test time a day at a time until the HumanReviewWorkflow exists, then decide like an advocate would."""
    for _ in range(MAX_DAYS_TO_WAIT_FOR_REVIEW):
        try:
            await send_decision(env.client, escalation_id, action, note)
            return
        except ReviewNotFound:
            await env.sleep(SECONDS_PER_DAY)
    raise AssertionError(f"{review_workflow_id(escalation_id)} never started")


async def test_default_scenario_schedules_delivery_after_primary_goes_silent(
    env: WorkflowEnvironment, scenario: Scenario, tmp_path: Path
) -> None:
    async with running_workers(env.client, scenario, tmp_path) as escalations_path:
        summary = await (await _start_case(env.client)).result()

    assert summary.outcome is CaseOutcome.DELIVERY_SCHEDULED
    assert [v.supplier.supplier_name for v in summary.sourcing.qualified] == [
        "Lincoln Park DME Services",
        "Prairie State Medical Supply",
    ]
    assert summary.order.status is OrderOutcomeStatus.VALID
    assert any("K0003" in p for p in summary.order.problems_seen)

    attempts = summary.fulfillment.attempts
    assert [(a.supplier_name, a.result) for a in attempts] == [
        ("Lincoln Park DME Services", HandoffResult.SILENT),
        ("Prairie State Medical Supply", HandoffResult.SCHEDULED),
    ]
    assert summary.fulfillment.delivery_eta_days == 4

    plan_contact, delivery_contact = summary.patient_contacts
    assert plan_contact.reached and plan_contact.escalation_id
    assert delivery_contact.reached and delivery_contact.escalation_id is None

    store = EscalationStore(escalations_path)
    callbacks = store.open_callbacks()
    assert [c.escalation_id for c in callbacks] == [plan_contact.escalation_id]

    complete_callback(store, plan_contact.escalation_id, "explained the 20% and folding")
    assert store.open_callbacks() == []
    assert store.get(plan_contact.escalation_id)["status"] == "completed"
    with pytest.raises(ReviewNotFound, match="already completed"):
        complete_callback(store, plan_contact.escalation_id, "again")


async def test_nobody_reachable_waits_for_human_then_closes(env: WorkflowEnvironment, tmp_path: Path) -> None:
    silent = Scenario(description="nobody answers", parties={})

    async with running_workers(env.client, silent, tmp_path) as escalations_path:
        handle = await _start_case(env.client)
        sourcing_escalation = f"{handle.id}/sourcing:no-supplier"
        await _decide_once_waiting(env, sourcing_escalation, HumanAction.CLOSE, "no local supplier; referred out")
        summary = await handle.result()

    assert summary.outcome is CaseOutcome.CLOSED_BY_HUMAN
    assert summary.closing_note == "no local supplier; referred out"
    assert summary.order.status is OrderOutcomeStatus.ESCALATED
    resolved = {e["escalation_id"]: e["status"] for e in json.loads(escalations_path.read_text())}
    assert resolved[sourcing_escalation] == "resolved"


async def test_pcp_needing_info_escalates_with_the_reason(env: WorkflowEnvironment, tmp_path: Path) -> None:
    scenario = load_scenario(SCENARIOS_DIR / "escalation.json")

    async with running_workers(env.client, scenario, tmp_path) as escalations_path:
        handle = await _start_case(env.client)
        order_escalation = f"{handle.id}/pcp-order:pcp-needs-info"
        await _decide_once_waiting(env, order_escalation, HumanAction.CLOSE, "PT eval being scheduled")
        summary = await handle.result()

        review = await env.client.get_workflow_handle_for(HumanReviewWorkflow.run, review_workflow_id(order_escalation)).result()
        assert review.action is HumanAction.CLOSE

    assert summary.outcome is CaseOutcome.CLOSED_BY_HUMAN
    assert summary.order.status is OrderOutcomeStatus.ESCALATED
    assert [v.supplier.supplier_name for v in summary.sourcing.qualified] == ["Prairie State Medical Supply"]
    assert order_escalation in {e["escalation_id"] for e in json.loads(escalations_path.read_text())}


class FlakyProvider:
    """LLM provider that is down until `recover()` is called."""

    def __init__(self) -> None:
        self.healthy = False

    def recover(self) -> None:
        self.healthy = True

    async def __call__(self, system_prompt: str, user_prompt: str, schema: type) -> object:
        if not self.healthy:
            raise LlmError("provider down: 503 from upstream")
        return await fake_complete(system_prompt, user_prompt, schema)


async def test_provider_outage_becomes_technical_review_and_retry_recovers(
    env: WorkflowEnvironment, scenario: Scenario, tmp_path: Path
) -> None:
    provider = FlakyProvider()

    async with running_workers(env.client, scenario, tmp_path, complete=provider) as escalations_path:
        handle = await _start_case(env.client)
        sourcing_review = f"{handle.id}:technical-sourcing"
        order_review = f"{handle.id}:technical-pcp-order"
        store = EscalationStore(escalations_path)
        while store.get(sourcing_review) is None or store.get(order_review) is None:
            await env.sleep(SECONDS_PER_DAY)

        provider.recover()
        await _decide_once_waiting(env, sourcing_review, HumanAction.RETRY, "OpenRouter back up")
        await _decide_once_waiting(env, order_review, HumanAction.RETRY, "OpenRouter back up")
        summary = await handle.result()

    assert summary.outcome is CaseOutcome.DELIVERY_SCHEDULED
    record = store.get(sourcing_review)
    assert record["reason"] == "Technical failure in sourcing: LlmError"
    assert "provider down" in record["context"]["error"]
    assert record["status"] == "resolved" and record["action"] == "retry"
    assert {sourcing_review, order_review} <= set(summary.escalations)


def _pcp_first_answer_scenario(transcript: str, scenario: Scenario) -> Scenario:
    pcp_phone = load_case().pcp.phone
    office = ScriptedParty(
        name="Sunrise Family Medicine",
        calls={CallPurpose.PCP_ORDER_REQUEST: [RawCall(disposition=CallDisposition.CONNECTED, transcript=transcript)]},
    )
    return Scenario(description="first answer", parties={pcp_phone: office}, written_orders=scenario.written_orders[1:])


async def _run_order_chase(env: WorkflowEnvironment, scenario: Scenario, tmp_path: Path):
    case = load_case()
    async with running_workers(env.client, scenario, tmp_path):
        return await env.client.execute_workflow(
            PcpOrderWorkflow.run,
            OrderInput(
                case_id=case.case_id,
                patient_name=case.patient.name,
                equipment=case.equipment,
                pcp=case.pcp,
                seconds_per_day=SECONDS_PER_DAY,
            ),
            id=f"test-order-{uuid.uuid4()}",
            task_queue=CASE_TASK_QUEUE,
        )


async def test_signed_on_first_call_checks_inbox_without_waiting(env: WorkflowEnvironment, scenario: Scenario, tmp_path: Path) -> None:
    first = _pcp_first_answer_scenario("Office: Good timing, Dr. Chen signed it this morning and we faxed it over.", scenario)

    result = await _run_order_chase(env, first, tmp_path)

    assert result.status is OrderOutcomeStatus.VALID
    assert result.nudges == 0


async def test_needs_info_on_first_call_escalates_immediately(env: WorkflowEnvironment, scenario: Scenario, tmp_path: Path) -> None:
    first = _pcp_first_answer_scenario("Office: Dr. Chen wants a mobility assessment from PT before she signs.", scenario)

    result = await _run_order_chase(env, first, tmp_path)

    assert result.status is OrderOutcomeStatus.ESCALATED
    assert result.nudges == 0
    assert result.escalation_id.endswith(":pcp-needs-info")
