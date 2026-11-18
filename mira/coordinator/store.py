"""Local document DB: one JSON document per case; locks, atomic replacement, version checks.

All writes go through this module, never through model filesystem tools. Requires a shared
local filesystem; this is not a distributed database or a multi-host deployment.
"""

import fcntl
import os
import re
import tempfile
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from mira.config import RUNTIME_DIR
from mira.coordinator.models import (
    ActionKind, AppointmentSlot, CaseDocument, EventKind, IncomingEvent, Party,
    ProposedAction, Snapshot, SyncRequest, TaskKind, TaskStatus, TurnRequest,
    RequirementNode,
)
from mira.coordinator.knowledge import forward, observe

TERMINAL = {TaskStatus.COMPLETED, TaskStatus.CANCELLED}
MAX_CONTACTS = 4  # consecutive contacts the contacted party has not answered
MAX_TASK_ACTIONS = 16  # all actions on one task since creation or the last human resume


class CoordinatorError(ValueError):
    pass


def snapshot(doc: CaseDocument) -> Snapshot:
    deadlines = [t.next_check_at for t in doc.tasks.values() if t.status is TaskStatus.WAITING and t.next_check_at]
    return Snapshot(
        version=doc.version, closed=doc.closed,
        ready=not doc.closed and any(t.status is TaskStatus.READY for t in doc.tasks.values()),
        next_check_at=min(deadlines) if deadlines and not doc.closed else None,
        tasks=list(doc.tasks.values()),
        last_action=doc.actions[-1] if doc.actions else None,
        knowledge=doc.knowledge,
    )


class JsonCaseStore:
    def __init__(self, root: Path = RUNTIME_DIR / "coordinator") -> None:
        self.root = root

    def _path(self, case_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,149}", case_id):
            raise CoordinatorError("case_id must be 1–150 letters, digits, dots, underscores or hyphens")
        return self.root / f"{case_id}.json"

    def read(self, case_id: str) -> CaseDocument:
        return CaseDocument.model_validate_json(self._path(case_id).read_text())

    def change(self, case_id: str, mutate: Callable[[CaseDocument | None], CaseDocument]) -> CaseDocument:
        path = self._path(case_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            old = self.read(case_id) if path.exists() else None
            new = mutate(old)
            if new == old:
                return new
            new = new.model_copy(update={"version": (old.version if old else 0) + 1})
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{case_id}-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as handle:
                    handle.write(new.model_dump_json(indent=2))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return new

        # Closing the lock file releases the cross-process lock, including on exceptions.

    def sync(self, request: SyncRequest) -> Snapshot:
        def mutate(old: CaseDocument | None) -> CaseDocument:
            doc = old or CaseDocument(
                case_id=request.inp.case_id, patient_name=request.inp.patient_name, contacts=request.inp.contacts,
                slots=[AppointmentSlot(
                    slot_id=f"slot-{i}", starts_at=request.now + timedelta(days=i + 2),
                    offer_expires_at=request.now + timedelta(days=1),
                ) for i in (1, 2)],
            )
            tasks = dict(doc.tasks)
            graph = doc.knowledge
            for task in [*request.inp.tasks, *request.tasks]:
                if task.task_id not in tasks and not doc.closed:
                    # Caller-supplied tasks cannot insert terminal state or fabricated booking receipts.
                    tasks[task.task_id] = type(task)(
                        task_id=task.task_id, kind=task.kind, goal=task.goal,
                        source_escalation_id=task.source_escalation_id,
                        requirement_request=task.requirement_request,
                    )
                    if task.kind is TaskKind.REQUIREMENT:
                        if task.requirement_request is None:
                            raise CoordinatorError("Requirement tasks require source-call provenance")
                        source = task.requirement_request
                        node = RequirementNode(task_id=task.task_id, requester_role=source.requester.role,
                                               requester_phone=source.requester.phone,
                                               description=source.requirement.description)
                        graph = graph.model_copy(update={"requirements": {**graph.requirements, task.task_id: node},
                            "edges": [*graph.edges, {"source": node.requester, "relation": "requires", "target": task.task_id,
                             "evidence": source.source_call_id, "workflow_id": source.requester_workflow_id}]})
                        if source.requirement.clinical_decision_required is not False:
                            tasks[task.task_id] = tasks[task.task_id].model_copy(update={
                                "status": TaskStatus.REVIEW, "review_reason": "Request requires clinical judgment or its administrative scope is unconfirmed"})
            events = list(doc.events)
            slots = list(doc.slots)
            for event in request.events:
                existing = next((e for e in events if e["event_id"] == event.event_id), None)
                if existing:
                    if {k: v for k, v in existing.items() if k != "received_at"} != event.model_dump(mode="json"):
                        raise CoordinatorError("event_id already exists with different content")
                    continue
                if event.task_id not in tasks:
                    raise CoordinatorError(f"Unknown task: {event.task_id}")
                validate_event(event, tasks[event.task_id].kind)
                events.append({**event.model_dump(mode="json"), "received_at": request.now.isoformat()})
                task = tasks[event.task_id]
                if task.status in TERMINAL or doc.closed:
                    continue  # Late replies are evidence, never automatic reopening.
                if event.kind is EventKind.SLOT_OFFERED:
                    slots = [s for s in slots if s.slot_id != event.slot_id]
                    slots.append(AppointmentSlot(slot_id=event.slot_id, starts_at=event.slot_starts_at,
                                                 offer_expires_at=event.offer_expires_at))
                if event.kind is EventKind.CANCEL:
                    tasks[event.task_id] = task.model_copy(update={"status": TaskStatus.CANCELLED, "next_check_at": None})
                elif event.kind is EventKind.RESUME:
                    tasks[event.task_id] = task.model_copy(update={
                        "status": TaskStatus.READY, "next_check_at": None, "waiting_for": None,
                        "review_reason": "", "contacts": 0, "actions_taken": 0,
                    })
                elif task.status is not TaskStatus.REVIEW:
                    # Only a reply from the party we contacted answers our contacts; others are new evidence.
                    answered = task.waiting_for is not None and event.party is task.waiting_for
                    tasks[event.task_id] = task.model_copy(update={
                        "status": TaskStatus.READY, "next_check_at": None,
                        "waiting_for": None if answered else task.waiting_for,
                        "contacts": 0 if answered else task.contacts,
                    })
                if event.kind is EventKind.OBSERVATION:
                    try:
                        graph, review = observe(doc.model_copy(update={"knowledge": graph}), task, event)
                    except ValueError as exc:
                        raise CoordinatorError(str(exc)) from exc
                    if review:
                        tasks[event.task_id] = tasks[event.task_id].model_copy(update={
                            "status": TaskStatus.REVIEW, "review_reason": review, "next_check_at": None})
            for key, task in tasks.items():
                if task.status is TaskStatus.WAITING and task.next_check_at and task.next_check_at <= request.now:
                    tasks[key] = task.model_copy(update={"status": TaskStatus.READY, "next_check_at": None})
            return doc.model_copy(update={"tasks": tasks, "events": events, "slots": slots, "knowledge": graph,
                                          "closed": doc.closed or request.close})
        return snapshot(self.change(request.inp.case_id, mutate))

    def review_tasks(self, request: TurnRequest, expected_version: int, task_ids: list[str], reason: str) -> Snapshot:
        """Move the named READY tasks to review in one turn; a retried turn is a no-op."""
        def mutate(doc: CaseDocument | None) -> CaseDocument:
            if doc is None:
                raise CoordinatorError("Case does not exist")
            if request.turn_id in doc.turns:
                return doc
            if doc.version != expected_version:
                raise CoordinatorError("Stale case version; re-read before pausing tasks")
            ready = [i for i in task_ids if i in doc.tasks and doc.tasks[i].status is TaskStatus.READY]
            if not ready:
                return doc
            tasks = dict(doc.tasks)
            records = []
            for task_id in ready:
                tasks[task_id] = tasks[task_id].model_copy(update={
                    "status": TaskStatus.REVIEW, "next_check_at": None, "review_reason": reason})
                records.append({"action_id": f"{request.turn_id}:{task_id}", "at": request.now.isoformat(),
                                **ProposedAction(task_id=task_id, kind=ActionKind.REVIEW, reason=reason).model_dump(mode="json")})
            return doc.model_copy(update={"tasks": tasks, "actions": [*doc.actions, *records],
                                          "turns": {**doc.turns, request.turn_id: records[0]}})
        return snapshot(self.change(request.case_id, mutate))

    def apply(self, request: TurnRequest, expected_version: int, action: ProposedAction) -> Snapshot:
        def mutate(doc: CaseDocument | None) -> CaseDocument:
            if doc is None:
                raise CoordinatorError("Case does not exist")
            if request.turn_id in doc.turns:
                return doc  # Activity retry after commit must not duplicate the mock action.
            if doc.version != expected_version:
                raise CoordinatorError("Stale case version; re-read before proposing another action")
            if doc.closed or action.task_id not in doc.tasks:
                raise CoordinatorError("Case closed or task unknown")
            task = doc.tasks[action.task_id]
            if task.status is not TaskStatus.READY:
                raise CoordinatorError("Only a ready task can be acted on")
            if action.kind is not ActionKind.REVIEW and task.actions_taken >= MAX_TASK_ACTIONS:
                raise CoordinatorError("Task exhausted its action budget; human review required")
            changes = {
                "status": TaskStatus.WAITING,
                "next_check_at": request.now + timedelta(seconds=request.reminder_seconds),
                "actions_taken": task.actions_taken + (0 if action.kind is ActionKind.REVIEW else 1),
            }
            evidence = next((e for e in doc.events if e["event_id"] == action.evidence_event_id and e["task_id"] == task.task_id), None)
            receipt: dict = {"mocked": True}
            graph = doc.knowledge
            source = task.requirement_request
            if source and source.requirement.clinical_decision_required is not False and action.kind is not ActionKind.REVIEW:
                raise CoordinatorError("Clinical requirements cannot be cleared by administrative actions")
            if action.kind is ActionKind.CONTACT:
                if action.party not in {Party.PATIENT, Party.PCP, Party.SUPPLIER} or not action.message.strip():
                    raise CoordinatorError("Contact requires an allowed party and a message")
                changes.update(waiting_for=action.party, contacts=task.contacts + 1)
                receipt["outbound_message"] = action.message
                if task.contacts >= MAX_CONTACTS:
                    changes.update(status=TaskStatus.REVIEW, next_check_at=None, review_reason="Contact budget exhausted")
                    receipt = {"mocked": True, "not_sent": "Contact budget exhausted"}
            elif action.kind is ActionKind.BOOK:
                if task.kind is not TaskKind.PT or task.booking_reference:
                    raise CoordinatorError("Booking requires an unbooked PT task")
                slot = next((s for s in doc.slots if s.slot_id == action.slot_id), None)
                if slot is None or slot.offer_expires_at <= request.now or slot.starts_at <= request.now:
                    raise CoordinatorError("Slot offer expired or unknown; ask the office for a new offer")
                if not evidence or evidence["kind"] != EventKind.SLOT_ACCEPTED or evidence["slot_id"] != action.slot_id:
                    raise CoordinatorError("Booking requires explicit patient acceptance of this exact slot")
                evidence_index = doc.events.index(evidence)
                if any(e["task_id"] == task.task_id and e["party"] == Party.PATIENT for e in doc.events[evidence_index + 1:]):
                    raise CoordinatorError("Patient sent a later message; clarify current consent before booking")
                if any(e["kind"] == EventKind.SLOT_OFFERED and e["slot_id"] == action.slot_id for e in doc.events[evidence_index + 1:]):
                    raise CoordinatorError("Slot offer changed after consent; obtain fresh patient acceptance")
                reference = f"mock-booking:{doc.case_id}:{task.task_id}"
                changes.update(booking_reference=reference, booked_slot_id=slot.slot_id, waiting_for=Party.PCP)
                receipt["booking_reference"] = reference
            elif action.kind is ActionKind.COMPLETE:
                if task.kind is TaskKind.REQUIREMENT:
                    requirement = graph.requirements[task.task_id]
                    if (requirement.status != "accepted" or not evidence or
                        evidence["event_id"] != requirement.accepted_event_id or
                        evidence["party"] != requirement.requester_role):
                        raise CoordinatorError("Requester must explicitly accept the exact forwarded information before this task completes")
                elif task.kind is TaskKind.PT:
                    if not evidence or evidence["kind"] != EventKind.REPORT_ACCEPTED:
                        raise CoordinatorError("PT follow-up requires PCP acknowledgment accepting the report")
                else:
                    quotes = [e for e in doc.events if e["task_id"] == task.task_id and e["kind"] == EventKind.QUOTE]
                    if not evidence or evidence["kind"] != EventKind.QUOTE_ACCEPTED or not quotes:
                        raise CoordinatorError("Cost follow-up requires an explicit supplier quote and patient acceptance")
                    if evidence["reference_event_id"] != quotes[-1]["event_id"]:
                        raise CoordinatorError("Patient must accept the latest quote, not a superseded quote")
                changes.update(status=TaskStatus.COMPLETED, next_check_at=None, waiting_for=None)
            elif action.kind is ActionKind.REVIEW:
                changes.update(status=TaskStatus.REVIEW, next_check_at=None, review_reason=action.reason)
            elif action.kind is ActionKind.FORWARD:
                if task.kind is not TaskKind.REQUIREMENT or action.party is None or action.party.value != source.requester.role:
                    raise CoordinatorError("Forwarding goes only to the party that made the requirement")
                try:
                    graph = forward(doc, task, evidence)
                except ValueError as exc:
                    raise CoordinatorError(str(exc)) from exc
                changes.update(waiting_for=action.party)
            tasks = {**doc.tasks, task.task_id: task.model_copy(update=changes)}
            record = {"action_id": request.turn_id, "at": request.now.isoformat(), **action.model_dump(mode="json"), **receipt}
            return doc.model_copy(update={
                "tasks": tasks, "knowledge": graph, "actions": [*doc.actions, record],
                "turns": {**doc.turns, request.turn_id: record},
            })
        return snapshot(self.change(request.case_id, mutate))


def validate_event(event: IncomingEvent, kind: TaskKind) -> None:
    if event.kind is EventKind.OBSERVATION and (kind is not TaskKind.REQUIREMENT or event.observation is None or event.party is Party.HUMAN):
        raise CoordinatorError("Coordination observations require a requirement task and an external-party observation")
    expected = {
        EventKind.SLOT_OFFERED: Party.PCP,
        EventKind.SLOT_ACCEPTED: Party.PATIENT, EventKind.VISIT_COMPLETED: Party.PCP,
        EventKind.REPORT_ACCEPTED: Party.PCP, EventKind.QUOTE: Party.SUPPLIER,
        EventKind.QUOTE_ACCEPTED: Party.PATIENT, EventKind.RESUME: Party.HUMAN, EventKind.CANCEL: Party.HUMAN,
    }
    if event.kind in expected and event.party is not expected[event.kind]:
        raise CoordinatorError(f"{event.kind} must come from {expected[event.kind]}")
    if event.kind in {EventKind.SLOT_OFFERED, EventKind.SLOT_ACCEPTED, EventKind.VISIT_COMPLETED, EventKind.REPORT_ACCEPTED} and kind is not TaskKind.PT:
        raise CoordinatorError("PT evidence cannot be applied to a cost task")
    if event.kind in {EventKind.QUOTE, EventKind.QUOTE_ACCEPTED} and kind is not TaskKind.COST:
        raise CoordinatorError("Quote evidence cannot be applied to a PT task")
    if event.kind is EventKind.SLOT_ACCEPTED and not event.slot_id:
        raise CoordinatorError("slot_accepted requires slot_id")
    if event.kind is EventKind.SLOT_OFFERED:
        if not event.slot_id or not event.slot_starts_at or not event.offer_expires_at:
            raise CoordinatorError("slot_offered requires slot_id, slot_starts_at, offer_expires_at")
        if event.slot_starts_at.tzinfo is None or event.offer_expires_at.tzinfo is None:
            raise CoordinatorError("Slot timestamps must include a timezone")
        if event.offer_expires_at >= event.slot_starts_at:
            raise CoordinatorError("Slot offer must expire before the appointment")
    if event.kind is EventKind.QUOTE and (event.amount_cents is None or event.accepts_assignment is None):
        raise CoordinatorError("quote requires amount_cents and accepts_assignment")
    if event.kind is EventKind.QUOTE_ACCEPTED and not event.reference_event_id:
        raise CoordinatorError("quote_accepted requires reference_event_id")
