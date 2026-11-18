from collections.abc import Awaitable, Callable

from temporalio import activity
from temporalio.exceptions import ApplicationError

from mira.coordinator.models import (
    CaseDocument, Decision, ProposedAction, Snapshot, SyncRequest, TaskStatus, TurnRequest,
)
from mira.coordinator.store import CoordinatorError, JsonCaseStore, snapshot

Planner = Callable[[CaseDocument], Awaitable[ProposedAction]]


class CoordinatorActivities:
    def __init__(self, store: JsonCaseStore | None = None, planner: Planner | None = None) -> None:
        self.store = store or JsonCaseStore()
        self.planner = planner

    @activity.defn
    async def sync(self, request: SyncRequest) -> Snapshot:
        try:
            # Isolate invalid inbound evidence instead of failing the entire long-running case.
            self.store.sync(request.model_copy(update={"events": [], "close": False}))
            for event in request.events:
                try:
                    self.store.sync(request.model_copy(update={"events": [event], "close": False}))
                except CoordinatorError as exc:
                    reason = str(exc)

                    def reject(doc: CaseDocument | None) -> CaseDocument:
                        assert doc is not None
                        record = {"event_id": event.event_id, "task_id": event.task_id, "reason": reason}
                        if record in doc.rejected_events:
                            return doc
                        return doc.model_copy(update={"rejected_events": [*doc.rejected_events, record]})

                    self.store.change(request.inp.case_id, reject)
            return self.store.sync(request.model_copy(update={"events": []}))
        except CoordinatorError as exc:
            raise ApplicationError(str(exc), type="InvalidCoordinatorEvent", non_retryable=True) from exc

    @activity.defn
    async def plan(self, request: TurnRequest) -> Decision:
        from mira.coordinator.agent import propose_next_action

        doc = self.store.read(request.case_id)
        action = await (self.planner or propose_next_action)(doc)
        return Decision(request=request, expected_version=doc.version, action=action)

    @activity.defn
    async def apply(self, decision: Decision) -> Snapshot:
        request, action = decision.request, decision.action
        doc = self.store.read(request.case_id)
        try:
            return self.store.apply(request, decision.expected_version, action)
        except CoordinatorError as exc:
            # A rejected proposal becomes a visible review, never a fabricated success.
            # Model output is not trusted to grant its own preconditions.
            if doc.version != decision.expected_version:
                return snapshot(doc)  # Concurrent updates invalidate proposals, not newer state.
            targeted = doc.tasks.get(action.task_id)
            if targeted is not None and targeted.status is TaskStatus.READY:
                return self.store.review_tasks(request, doc.version, [action.task_id], f"Proposal rejected: {exc}")
            # The proposal names no ready task, so it cannot be pinned on one. Re-planning would
            # likely repeat it; pause every ready task with the reason instead.
            return self.store.review_tasks(request, doc.version, _ready_task_ids(doc),
                                           f"Coordinator proposed an action for unknown or non-ready task {action.task_id!r}: {exc}")

    @activity.defn
    async def failure(self, request: TurnRequest) -> Snapshot:
        doc = self.store.read(request.case_id)
        if request.turn_id in doc.turns:
            return snapshot(doc)
        # A failed turn chose no task, so every task waiting on the model is equally blocked.
        return self.store.review_tasks(request, doc.version, _ready_task_ids(doc),
            "Coordinator model unavailable or turn failed after retries; inspect worker logs and resume after recovery")


def _ready_task_ids(doc: CaseDocument) -> list[str]:
    return [task.task_id for task in doc.tasks.values() if task.status is TaskStatus.READY]
