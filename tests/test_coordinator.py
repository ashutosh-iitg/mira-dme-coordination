from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from mira.coordinator.activities import CoordinatorActivities
from mira.coordinator.agent import propose_next_action
from mira.coordinator.models import (
    ActionKind, CoordinatorInput, EventKind, FollowupTask, IncomingEvent, Party,
    ProposedAction, SyncRequest, TaskKind, TaskStatus, TurnRequest,
)
from mira.coordinator.store import CoordinatorError, JsonCaseStore
from mira.coordinator.workflow import TASK_QUEUE, CaseCoordinatorWorkflow

NOW = datetime(2026, 11, 18, 10, tzinfo=UTC)


def setup_case(tmp_path: Path, kind=TaskKind.PT):
    store = JsonCaseStore(tmp_path)
    inp = CoordinatorInput(case_id="eleanor", patient_name="Eleanor Martinez", tasks=[
        FollowupTask(task_id="followup", kind=kind, goal="Coordinate a PCP-requested report"),
    ])
    store.sync(SyncRequest(inp=inp, now=NOW))
    return store, inp


def apply(store, action, now=NOW, turn_id=None):
    doc = store.read("eleanor")
    return store.apply(TurnRequest(
        case_id=doc.case_id, turn_id=turn_id or str(uuid4()), now=now, reminder_seconds=86400,
    ), doc.version, action)


def event(event_id="e1", kind=EventKind.MESSAGE, party=Party.PATIENT, **kwargs):
    return IncomingEvent(event_id=event_id, task_id="followup", kind=kind, party=party, text="Demo confirmation", **kwargs)


def test_json_restart_and_duplicate_inbound_event(tmp_path):
    store, inp = setup_case(tmp_path)
    request = SyncRequest(inp=inp, now=NOW, events=[event()])
    first = store.sync(request)
    restarted = JsonCaseStore(tmp_path)
    assert restarted.sync(request).version == first.version
    assert len(restarted.read("eleanor").events) == 1
    changed = event().model_copy(update={"text": "different content"})
    with pytest.raises(CoordinatorError, match="different content"):
        restarted.sync(SyncRequest(inp=inp, now=NOW, events=[changed]))


def test_cannot_book_from_plain_message_or_expired_consent(tmp_path):
    store, inp = setup_case(tmp_path)
    action = ProposedAction(task_id="followup", kind=ActionKind.BOOK, reason="Book requested visit", slot_id="slot-1", evidence_event_id="e1")
    store.sync(SyncRequest(inp=inp, now=NOW, events=[event()]))
    with pytest.raises(CoordinatorError, match="explicit patient acceptance"):
        apply(store, action)
    consent = event("consent", EventKind.SLOT_ACCEPTED, slot_id="slot-1")
    store.sync(SyncRequest(inp=inp, now=NOW, events=[consent]))
    with pytest.raises(CoordinatorError, match="expired"):
        apply(store, action.model_copy(update={"evidence_event_id": "consent"}), now=NOW + timedelta(days=2))


def test_booking_receipt_is_idempotent_but_is_not_report_completion(tmp_path):
    store, inp = setup_case(tmp_path)
    consent = event("consent", EventKind.SLOT_ACCEPTED, slot_id="slot-1")
    store.sync(SyncRequest(inp=inp, now=NOW, events=[consent]))
    before = store.read("eleanor")
    action = ProposedAction(task_id="followup", kind=ActionKind.BOOK, reason="Patient accepted", slot_id="slot-1", evidence_event_id="consent")
    request = TurnRequest(case_id="eleanor", turn_id="same-turn", now=NOW, reminder_seconds=86400)
    store.apply(request, before.version, action)
    store.apply(request, before.version, action)
    doc = store.read("eleanor")
    assert len(doc.actions) == 1
    assert doc.tasks["followup"].booking_reference
    store.sync(SyncRequest(inp=inp, now=NOW, events=[event("arrived", EventKind.VISIT_COMPLETED, Party.PCP)]))
    with pytest.raises(CoordinatorError, match="PCP acknowledgment"):
        apply(store, ProposedAction(task_id="followup", kind=ActionKind.COMPLETE, reason="Visited", evidence_event_id="arrived"))


def test_only_pcp_can_attest_report_acceptance_and_late_replies_do_not_reopen(tmp_path):
    store, inp = setup_case(tmp_path)
    with pytest.raises(CoordinatorError, match="must come from pcp"):
        store.sync(SyncRequest(inp=inp, now=NOW, events=[event(kind=EventKind.REPORT_ACCEPTED)]))
    accepted = event("report", EventKind.REPORT_ACCEPTED, Party.PCP)
    store.sync(SyncRequest(inp=inp, now=NOW, events=[accepted]))
    apply(store, ProposedAction(task_id="followup", kind=ActionKind.COMPLETE, reason="PCP accepted report", evidence_event_id="report"))
    store.sync(SyncRequest(inp=inp, now=NOW + timedelta(days=2), events=[event("late")]))
    assert store.read("eleanor").tasks["followup"].status is TaskStatus.COMPLETED


def test_quote_acceptance_must_reference_latest_supplier_quote(tmp_path):
    store, inp = setup_case(tmp_path, TaskKind.COST)
    quote1 = event("quote1", EventKind.QUOTE, Party.SUPPLIER, amount_cents=2000, accepts_assignment=True)
    accepted = event("accepted", EventKind.QUOTE_ACCEPTED, reference_event_id="quote1")
    quote2 = quote1.model_copy(update={"event_id": "quote2", "amount_cents": 2500})
    store.sync(SyncRequest(inp=inp, now=NOW, events=[quote1, accepted, quote2]))
    with pytest.raises(CoordinatorError, match="latest quote"):
        apply(store, ProposedAction(task_id="followup", kind=ActionKind.COMPLETE, reason="Patient agreed", evidence_event_id="accepted"))


def test_old_proposal_does_not_overwrite_newer_document(tmp_path):
    store, inp = setup_case(tmp_path)
    version = store.read("eleanor").version
    store.sync(SyncRequest(inp=inp, now=NOW, events=[event()]))
    with pytest.raises(CoordinatorError, match="Stale"):
        store.apply(TurnRequest(case_id="eleanor", turn_id="old", now=NOW, reminder_seconds=86400), version,
                    ProposedAction(task_id="followup", kind=ActionKind.WAIT, reason="Wait"))


def test_path_traversal_is_rejected(tmp_path):
    with pytest.raises(CoordinatorError, match="case_id"):
        JsonCaseStore(tmp_path).read("../secrets")


def test_fresh_office_offer_recovers_expired_slot_and_later_patient_message_invalidates_consent(tmp_path):
    store, inp = setup_case(tmp_path)
    later = NOW + timedelta(days=2)
    offer = event("offer", EventKind.SLOT_OFFERED, Party.PCP, slot_id="new-slot",
                  slot_starts_at=later + timedelta(days=3), offer_expires_at=later + timedelta(days=1))
    consent = event("consent", EventKind.SLOT_ACCEPTED, slot_id="new-slot")
    store.sync(SyncRequest(inp=inp, now=later, events=[offer, consent, event("changed-mind")]))
    action = ProposedAction(task_id="followup", kind=ActionKind.BOOK, reason="Book", slot_id="new-slot", evidence_event_id="consent")
    with pytest.raises(CoordinatorError, match="later message"):
        apply(store, action, now=later)
    store.sync(SyncRequest(inp=inp, now=later, events=[consent.model_copy(update={"event_id": "reconfirmed"})]))
    apply(store, action.model_copy(update={"evidence_event_id": "reconfirmed"}), now=later)
    assert store.read("eleanor").tasks["followup"].booked_slot_id == "new-slot"


async def test_rejected_model_action_and_provider_failure_become_reviews(tmp_path):
    store, inp = setup_case(tmp_path)
    activities = CoordinatorActivities(store)
    from mira.coordinator.models import Decision
    request = TurnRequest(case_id="eleanor", turn_id="rejected", now=NOW, reminder_seconds=86400)
    result = await activities.apply(Decision(request=request, expected_version=store.read("eleanor").version,
        action=ProposedAction(task_id="followup", kind=ActionKind.COMPLETE, reason="Unsupported completion")))
    assert result.tasks[0].status is TaskStatus.REVIEW
    assert "Proposal rejected" in result.tasks[0].review_reason
    store.sync(SyncRequest(inp=inp, now=NOW, events=[event("resume", EventKind.RESUME, Party.HUMAN)]))
    failed = await activities.failure(request.model_copy(update={"turn_id": "provider-outage"}))
    assert failed.tasks[0].status is TaskStatus.REVIEW
    assert "after retries" in failed.tasks[0].review_reason


async def test_bad_inbound_evidence_is_recorded_without_stopping_supervision(tmp_path):
    store, inp = setup_case(tmp_path)
    activities = CoordinatorActivities(store)
    result = await activities.sync(SyncRequest(inp=inp, now=NOW, events=[
        event("bad", EventKind.REPORT_ACCEPTED, Party.PATIENT), event("valid-reply"),
    ]))
    doc = store.read("eleanor")
    assert result.ready
    assert doc.rejected_events[0]["event_id"] == "bad"
    assert [e["event_id"] for e in doc.events] == ["valid-reply"]


class ToolCallingModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


async def test_actual_deep_agents_graph_proposes_through_tool(tmp_path):
    store, _ = setup_case(tmp_path)
    action = ProposedAction(task_id="followup", kind=ActionKind.CONTACT, party=Party.PATIENT,
                            message="When are you available for the PCP-requested assessment?", reason="Collect availability")
    model = ToolCallingModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "proposal-1", "name": "propose_action", "args": action.model_dump(mode="json")}]),
        AIMessage(content="Proposed contacting the patient."),
    ])
    assert await propose_next_action(store.read("eleanor"), model=model) == action
    assert store.read("eleanor").actions == []  # Planning cannot commit state.


async def test_temporal_monitors_reply_days_later_and_accepts_new_tasks(tmp_path):
    store = JsonCaseStore(tmp_path)

    async def planner(doc):
        task = next(t for t in doc.tasks.values() if t.status is TaskStatus.READY)
        accepted = next((e for e in doc.events if e["task_id"] == task.task_id and e["kind"] == EventKind.REPORT_ACCEPTED), None)
        if accepted:
            action = ProposedAction(task_id=task.task_id, kind=ActionKind.COMPLETE, reason="PCP accepted report", evidence_event_id=accepted["event_id"])
        else:
            action = ProposedAction(task_id=task.task_id, kind=ActionKind.CONTACT, party=Party.PATIENT,
                                    message="Please share your availability", reason="Arrange requested assessment")
        model = ToolCallingModel(responses=[
            AIMessage(content="", tool_calls=[{"id": "decision", "name": "propose_action", "args": action.model_dump(mode="json")}]),
            AIMessage(content="Proposed the next action."),
        ])
        return await propose_next_action(doc, model=model)

    activities = CoordinatorActivities(store, planner)
    async with await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env:
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[CaseCoordinatorWorkflow],
                          activities=[activities.sync, activities.plan, activities.apply, activities.failure]):
            inp = CoordinatorInput(case_id="eleanor", patient_name="Eleanor", tasks=[
                FollowupTask(task_id="followup", kind=TaskKind.PT, goal="Obtain requested assessment report"),
            ])
            handle = await env.client.start_workflow(CaseCoordinatorWorkflow.run, inp, id=str(uuid4()), task_queue=TASK_QUEUE)

            async def until(predicate):
                for _ in range(100):
                    state = await handle.query(CaseCoordinatorWorkflow.status)
                    if state and predicate(state):
                        return state
                    await env.sleep(1)
                raise AssertionError("Coordinator never reached expected state")

            await until(lambda s: s.tasks[0].status is TaskStatus.WAITING)
            await env.sleep(timedelta(days=2))  # Durable reminder wakes and persists more mock outreach.
            await handle.signal(CaseCoordinatorWorkflow.receive, event("report", EventKind.REPORT_ACCEPTED, Party.PCP))
            await until(lambda s: s.tasks[0].status is TaskStatus.COMPLETED)
            await handle.signal(CaseCoordinatorWorkflow.add_task, FollowupTask(task_id="new", kind=TaskKind.COST, goal="Clarify supplier quote"))
            await until(lambda s: any(t.task_id == "new" and t.status is TaskStatus.WAITING for t in s.tasks))
            await handle.signal(CaseCoordinatorWorkflow.close)
            result = await handle.result()
            assert result.closed
            restarted = JsonCaseStore(tmp_path).read("eleanor")
            assert restarted.tasks["followup"].status is TaskStatus.COMPLETED
            assert len(restarted.actions) >= 3
