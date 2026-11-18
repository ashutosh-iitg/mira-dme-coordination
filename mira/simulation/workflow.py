import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from mira.coordinator.models import Snapshot, TaskStatus
    from mira.coordinator.workflow import CaseCoordinatorWorkflow
    from mira.models import CaseInput, Escalation, HumanAction, HumanDecision, HumanReviewInput
    from mira.simulation.activities import SimulationActivities
    from mira.simulation.models import ActionRequest, SimulationEvent, SimulationInput, SimulationResult, StageRequest
    from mira.workflows.case import CaseWorkflow
    from mira.workflows.human_review import HumanReviewWorkflow

RETRY = RetryPolicy(maximum_attempts=3)
TIMEOUT = timedelta(seconds=30)


@workflow.defn
class SimulatedDmeCaseWorkflow:
    """Durably links mock conversations, the coordinator and the existing DME case runner."""
    def __init__(self) -> None:
        self._notices: list[Snapshot] = []
        self._seen: set[str] = set()

    @workflow.signal
    def coordinator_action(self, state: Snapshot) -> None:
        action_id = (state.last_action or {}).get("action_id")
        if action_id and action_id not in self._seen:
            self._seen.add(action_id)
            self._notices.append(state)

    async def _record(self, inp: SimulationInput, event_kind: str, **details) -> None:
        await workflow.execute_activity_method(
            SimulationActivities.record,
            SimulationEvent(case_id=inp.case.case_id, business_time=workflow.now(), kind=event_kind, details=details),
            start_to_close_timeout=TIMEOUT, retry_policy=RETRY,
        )

    async def _deliver(self, inp: SimulationInput, index: int):
        stage = inp.scenario["stages"][index]
        delay = float(stage.get("advance_hours", 0))
        if delay:
            await self._record(inp, "business_timer_scheduled", stage=stage["name"], delay_hours=delay)
            await workflow.sleep(timedelta(hours=delay))
            await self._record(inp, "business_timer_fired", stage=stage["name"])
        return await workflow.execute_activity_method(
            SimulationActivities.deliver_stage,
            StageRequest(inp=inp, stage_index=index, business_time=workflow.now()),
            start_to_close_timeout=TIMEOUT, retry_policy=RETRY,
        )

    @workflow.run
    async def run(self, inp: SimulationInput) -> SimulationResult:
        await workflow.execute_activity_method(
            SimulationActivities.prepare, inp, start_to_close_timeout=TIMEOUT, retry_policy=RETRY,
        )
        root_id = workflow.info().workflow_id
        coordinator_id = f"{root_id}/coordinator"
        ids = [root_id, coordinator_id]
        initial = await self._deliver(inp, 0)
        child = await workflow.start_child_workflow(
            CaseCoordinatorWorkflow.run,
            inp.coordinator.model_copy(update={
                "initial_events": initial, "observer_workflow_id": root_id, "stop_when_settled": True,
            }),
            id=coordinator_id,
        )
        checks = []
        for index, stage in enumerate(inp.scenario["stages"]):
            if index:
                events = await self._deliver(inp, index)
                await child.signal(CaseCoordinatorWorkflow.receive_many, events)
            try:
                await workflow.wait_condition(lambda: len(self._notices) > index, timeout=timedelta(hours=2))
            except asyncio.TimeoutError:
                checks.append({"stage": stage["name"], "passed": False, "error": "No coordinator action before simulation deadline"})
                await self._record(inp, "stage_failed", **checks[-1])
                break
            state = self._notices[index]
            await workflow.execute_activity_method(
                SimulationActivities.dispatch_action,
                ActionRequest(case_id=inp.case.case_id, business_time=workflow.now(), snapshot=state),
                start_to_close_timeout=TIMEOUT, retry_policy=RETRY,
            )
            action = state.last_action or {}
            passed = action.get("kind") in stage["expected_kinds"] and (
                not stage.get("expected_parties") or action.get("party") in stage["expected_parties"]
            ) and not action.get("not_sent")
            checks.append({"stage": stage["name"], "passed": bool(passed),
                           "kind": action.get("kind"), "party": action.get("party"),
                           "business_time_utc": workflow.now().isoformat()})
            await self._record(inp, "stage_checked", **checks[-1])
            if state.tasks[0].status is TaskStatus.REVIEW:
                break
        # End supervision explicitly if a wrong action left the final task waiting.
        # This does not manufacture completion or erase the failed check.
        if not self._notices or self._notices[-1].tasks[0].status not in {
            TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.REVIEW,
        }:
            await child.signal(CaseCoordinatorWorkflow.close)
        final = await child
        expected = inp.scenario["expected_final_status"]
        coordinator_passed = all(c["passed"] for c in checks) and len(checks) == len(inp.scenario["stages"]) and final.tasks[0].status.value == expected
        if final.tasks[0].status is TaskStatus.COMPLETED:
            await self._record(inp, "dme_case_unblocked", prerequisite="Coordinator follow-up completed with required evidence")
            dme_id = f"{root_id}/dme"
            ids.extend([dme_id, f"{dme_id}/sourcing", f"{dme_id}/pcp-order", f"{dme_id}/fulfillment"])
            try:
                dme = await workflow.execute_child_workflow(
                    CaseWorkflow.run, CaseInput(case=inp.case, seconds_per_day=60), id=dme_id,
                    execution_timeout=timedelta(hours=2),
                )
            except ChildWorkflowError as error:
                await self._record(inp, "simulation_finished", outcome="dme_failed", error=str(error))
                return SimulationResult(scenario_id=inp.scenario["id"], outcome="dme_failed",
                    coordinator=final, stage_checks=checks, workflow_ids=ids, passed=False)
            await self._record(inp, "simulation_finished", outcome=dme.outcome,
                               coordinator_passed=coordinator_passed, supplier=dme.fulfillment.scheduled_with.supplier.supplier_name if dme.fulfillment else None)
            return SimulationResult(scenario_id=inp.scenario["id"], outcome=dme.outcome.value,
                coordinator=final, stage_checks=checks, dme=dme, workflow_ids=ids,
                passed=coordinator_passed and dme.outcome.value == "delivery_scheduled")

        review_id = f"{root_id}/clinical-review"
        ids.append(review_id)
        escalation = Escalation(escalation_id=f"{root_id}:needs-review", case_id=inp.case.case_id,
                               reason="Follow-up requires human review; DME handoff is blocked")
        review = await workflow.start_child_workflow(
            HumanReviewWorkflow.run, HumanReviewInput(case_id=inp.case.case_id, escalation=escalation), id=review_id,
        )
        await self._record(inp, "human_review_opened", review_workflow_id=review_id,
                           agent_requested_review=final.tasks[0].status is TaskStatus.REVIEW)
        await workflow.sleep(timedelta(hours=1))
        await self._record(inp, "mock_human_decision", action="close", note="Clinical request referred to clinician; no automatic prescription change")
        await review.signal(HumanReviewWorkflow.decide, HumanDecision(
            escalation_id=escalation.escalation_id, action=HumanAction.CLOSE,
            note="Mock clinician/advocate accepts handoff; case closed for human clinical handling",
        ))
        await review
        await self._record(inp, "simulation_finished", outcome="human_review_required", coordinator_passed=coordinator_passed)
        return SimulationResult(scenario_id=inp.scenario["id"], outcome="human_review_required",
            coordinator=final, stage_checks=checks, workflow_ids=ids, passed=coordinator_passed)
