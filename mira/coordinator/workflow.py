import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from mira.coordinator.activities import CoordinatorActivities
    from mira.coordinator.models import (
        CoordinatorInput, FollowupTask, IncomingEvent, Snapshot, SyncRequest, TaskStatus, TurnRequest,
    )

TASK_QUEUE = "dme-coordinator"
IO_TIMEOUT = timedelta(seconds=30)
MODEL_TIMEOUT = timedelta(minutes=3)
RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=3)


@workflow.defn
class CaseCoordinatorWorkflow:
    """Supervises an ongoing business case; model turns are bounded, waiting is durable."""

    def __init__(self) -> None:
        self._events: list[IncomingEvent] = []
        self._tasks: list[FollowupTask] = []
        self._closing = False
        self._snapshot: Snapshot | None = None

    @workflow.signal
    def receive(self, event: IncomingEvent) -> None:
        self._events.append(event)

    @workflow.signal
    def receive_many(self, events: list[IncomingEvent]) -> None:
        self._events.extend(events)

    @workflow.signal
    def add_task(self, task: FollowupTask) -> None:
        self._tasks.append(task)

    @workflow.signal
    def close(self) -> None:
        self._closing = True

    @workflow.query
    def status(self) -> Snapshot | None:
        return self._snapshot

    @workflow.run
    async def run(self, inp: CoordinatorInput) -> Snapshot:
        self._events = [*inp.initial_events, *self._events]
        while True:
            events, self._events = self._events, []
            tasks, self._tasks = self._tasks, []
            self._snapshot = await workflow.execute_activity_method(
                CoordinatorActivities.sync,
                SyncRequest(inp=inp, now=workflow.now(), events=events, tasks=tasks, close=self._closing),
                start_to_close_timeout=IO_TIMEOUT, retry_policy=RETRY,
            )
            if self._snapshot.closed:
                return self._snapshot
            if inp.stop_when_settled and self._snapshot.tasks and all(
                t.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.REVIEW}
                for t in self._snapshot.tasks
            ):
                return self._snapshot
            if self._events or self._tasks or self._closing:
                continue  # Apply inbound state before a fresh agent decision.
            if self._snapshot.ready:
                request = TurnRequest(
                    case_id=inp.case_id, turn_id=str(workflow.uuid4()),
                    now=workflow.now(), reminder_seconds=inp.reminder_seconds,
                )
                workflow.set_current_details("Coordinator is reviewing open follow-ups")
                try:
                    decision = await workflow.execute_activity_method(
                        CoordinatorActivities.plan, request,
                        start_to_close_timeout=MODEL_TIMEOUT, retry_policy=RETRY,
                    )
                    if self._events or self._tasks or self._closing:
                        continue  # Discard an old plan if a reply arrived while the model was thinking.
                    self._snapshot = await workflow.execute_activity_method(
                        CoordinatorActivities.apply, decision,
                        start_to_close_timeout=IO_TIMEOUT, retry_policy=RETRY,
                    )
                except ActivityError:
                    self._snapshot = await workflow.execute_activity_method(
                        CoordinatorActivities.failure, request,
                        start_to_close_timeout=IO_TIMEOUT, retry_policy=RETRY,
                    )
                if inp.observer_workflow_id:
                    await workflow.get_external_workflow_handle(inp.observer_workflow_id).signal(
                        "coordinator_action", self._snapshot,
                    )
                continue
            workflow.set_current_details("Monitoring: waiting for a reply, a new follow-up, or a reminder deadline")
            timeout = None
            if self._snapshot.next_check_at:
                timeout = max(self._snapshot.next_check_at - workflow.now(), timedelta(milliseconds=1))
            try:
                await workflow.wait_condition(
                    lambda: bool(self._events or self._tasks or self._closing), timeout=timeout,
                )
            except asyncio.TimeoutError:
                pass  # Sync wakes tasks whose reminder deadline has passed.
            if workflow.info().is_continue_as_new_suggested() and not self._events and not self._tasks and not self._closing:
                workflow.continue_as_new(inp)  # JSON holds tasks/receipts; workflow history stays bounded.
