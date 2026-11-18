"""Regression tests: fallback reviews target the right task, and contact/action budgets cannot be reset by chatter."""

from uuid import uuid4

import pytest

from mira.coordinator.activities import CoordinatorActivities
from mira.coordinator.models import (
    ActionKind,
    CoordinatorInput,
    Decision,
    EventKind,
    FollowupTask,
    IncomingEvent,
    Party,
    ProposedAction,
    SyncRequest,
    TaskKind,
    TaskStatus,
    TurnRequest,
)
from mira.coordinator.store import (
    MAX_CONTACTS,
    MAX_TASK_ACTIONS,
    CoordinatorError,
    JsonCaseStore,
)
from tests.test_coordinator import NOW

CASE_ID = "two-tasks"


def two_task_case(tmp_path):
    store = JsonCaseStore(tmp_path)
    inp = CoordinatorInput(case_id=CASE_ID, patient_name="Eleanor Martinez", tasks=[
        FollowupTask(task_id="pt", kind=TaskKind.PT, goal="Coordinate the PCP-requested report"),
        FollowupTask(task_id="cost", kind=TaskKind.COST, goal="Clarify the patient's cost share"),
    ])
    store.sync(SyncRequest(inp=inp, now=NOW))
    return store, inp


def turn() -> TurnRequest:
    return TurnRequest(case_id=CASE_ID, turn_id=str(uuid4()), now=NOW, reminder_seconds=60)


def statuses(store) -> dict[str, TaskStatus]:
    return {task_id: task.status for task_id, task in store.read(CASE_ID).tasks.items()}


def contact(store, task_id="cost", party=Party.SUPPLIER):
    return store.apply(turn(), store.read(CASE_ID).version, ProposedAction(
        task_id=task_id, kind=ActionKind.CONTACT, reason="Ask for a quote", party=party, message="What is the quote?"))


def reply(store, inp, party, event_id, task_id="cost"):
    event = IncomingEvent(event_id=event_id, task_id=task_id, party=party, kind=EventKind.MESSAGE, text="Reply")
    return store.sync(SyncRequest(inp=inp, now=NOW, events=[event]))


async def test_rejected_proposal_reviews_only_the_task_it_targeted(tmp_path):
    store, _ = two_task_case(tmp_path)
    invalid = ProposedAction(task_id="cost", kind=ActionKind.COMPLETE, reason="No quote evidence yet")

    await CoordinatorActivities(store).apply(Decision(request=turn(), expected_version=store.read(CASE_ID).version, action=invalid))

    assert statuses(store) == {"pt": TaskStatus.READY, "cost": TaskStatus.REVIEW}
    assert "Proposal rejected" in store.read(CASE_ID).tasks["cost"].review_reason


async def test_proposal_for_unknown_task_pauses_all_ready_tasks_with_the_reason(tmp_path):
    store, _ = two_task_case(tmp_path)
    invalid = ProposedAction(task_id="no-such-task", kind=ActionKind.WAIT, reason="Hallucinated task")

    await CoordinatorActivities(store).apply(Decision(request=turn(), expected_version=store.read(CASE_ID).version, action=invalid))

    doc = store.read(CASE_ID)
    assert statuses(store) == {"pt": TaskStatus.REVIEW, "cost": TaskStatus.REVIEW}
    assert all("no-such-task" in task.review_reason for task in doc.tasks.values())


async def test_failed_turn_reviews_every_ready_task_and_is_idempotent(tmp_path):
    store, _ = two_task_case(tmp_path)
    contact(store, task_id="pt", party=Party.PCP)  # pt is now waiting, not ready
    request = turn()
    activities = CoordinatorActivities(store)

    first = await activities.failure(request)
    retried = await activities.failure(request)

    assert statuses(store) == {"pt": TaskStatus.WAITING, "cost": TaskStatus.REVIEW}
    assert retried.version == first.version


def test_reply_from_another_party_does_not_reset_unanswered_contacts(tmp_path):
    store, inp = two_task_case(tmp_path)
    contact(store)

    reply(store, inp, Party.PATIENT, "patient-chatter")

    task = store.read(CASE_ID).tasks["cost"]
    assert task.contacts == 1
    assert task.waiting_for is Party.SUPPLIER


def test_reply_from_contacted_party_resets_unanswered_contacts(tmp_path):
    store, inp = two_task_case(tmp_path)
    contact(store)

    reply(store, inp, Party.SUPPLIER, "supplier-answer")

    task = store.read(CASE_ID).tasks["cost"]
    assert task.contacts == 0
    assert task.waiting_for is None


def test_unanswered_contact_budget_moves_task_to_review(tmp_path):
    store, inp = two_task_case(tmp_path)
    for attempt in range(MAX_CONTACTS + 1):
        contact(store)
        reply(store, inp, Party.PATIENT, f"chatter-{attempt}")  # wakes the task but never answers the supplier

    task = store.read(CASE_ID).tasks["cost"]
    assert task.status is TaskStatus.REVIEW
    assert task.review_reason == "Contact budget exhausted"


def test_answered_contacts_are_still_bounded_by_the_task_action_budget(tmp_path):
    store, inp = two_task_case(tmp_path)
    for attempt in range(MAX_TASK_ACTIONS):
        contact(store)
        reply(store, inp, Party.SUPPLIER, f"answer-{attempt}")

    with pytest.raises(CoordinatorError, match="action budget"):
        contact(store)

    resume = IncomingEvent(event_id="resume", task_id="cost", party=Party.HUMAN, kind=EventKind.RESUME, text="Keep going")
    store.sync(SyncRequest(inp=inp, now=NOW, events=[resume]))
    contact(store)
    assert store.read(CASE_ID).tasks["cost"].actions_taken == 1
