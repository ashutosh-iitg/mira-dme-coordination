import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from temporalio import workflow
from temporalio.exceptions import ActivityError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from mira.case_activities import CaseActivities
    from mira.models import (
        CaseInput,
        CaseOutcome,
        CaseStatus,
        CaseSummary,
        FulfillmentInput,
        FulfillmentResult,
        HumanAction,
        HumanReviewInput,
        OrderInput,
        OrderOutcomeStatus,
        OrderResult,
        PatientContact,
        PendingReview,
        ResolveEscalation,
        SourcingInput,
        SourcingResult,
    )
    from mira.workflows.common import CASE_ACTIVITY_TIMEOUT, DEFAULT_RETRY, escalate, log_event
    from mira.workflows.fulfillment import FulfillmentWorkflow
    from mira.workflows.human_review import HumanReviewWorkflow, review_workflow_id
    from mira.workflows.patient import PatientLiaison, delivery_points, plan_points
    from mira.workflows.pcp_order import PcpOrderWorkflow
    from mira.workflows.sourcing import SupplierSourcingWorkflow


StepResult = TypeVar("StepResult", SourcingResult, OrderResult, FulfillmentResult)
MAX_ERROR_DETAIL_CHARS = 300


def failure_category(error: BaseException) -> tuple[str, str]:
    """Walk Temporal's failure chain to the root cause, e.g. ChildWorkflowError -> ActivityError -> LlmError."""
    root = error
    while getattr(root, "cause", None) is not None:
        root = root.cause
    return getattr(root, "type", None) or type(root).__name__, str(root)[:MAX_ERROR_DETAIL_CHARS]


class CaseClosed(Exception):
    def __init__(self, note: str) -> None:
        super().__init__(note)
        self.note = note


@workflow.defn
class CaseWorkflow:
    """One per patient case: source + order in parallel, hand off, keep the patient informed.

    Blocking escalations start a HumanReviewWorkflow child and pause until a care advocate decides (retry or close).
    """

    def __init__(self) -> None:
        self._stage = "starting"
        self._pending_reviews: list[PendingReview] = []
        self._escalations: list[str] = []
        self._child_runs: dict[str, int] = {}
        self._sourcing: SourcingResult | None = None
        self._order: OrderResult | None = None
        self._fulfillment: FulfillmentResult | None = None
        self._contacts: list[PatientContact] = []
        self._patient = PatientLiaison()
        self._case_id = ""

    @workflow.query
    def status(self) -> CaseStatus:
        scheduled = self._fulfillment.scheduled_with if self._fulfillment else None
        return CaseStatus(
            stage=self._stage,
            pending_reviews=list(self._pending_reviews),
            supplier=scheduled.supplier.supplier_name if scheduled else None,
            delivery_eta_days=self._fulfillment.delivery_eta_days if self._fulfillment else None,
        )

    @workflow.run
    async def run(self, inp: CaseInput) -> CaseSummary:
        self._case_id = inp.case.case_id
        await log_event(inp.case.case_id, "case_started", patient=inp.case.patient.name, item=inp.case.equipment.hcpcs_code)
        try:
            await self._work_case(inp)
            outcome, note = CaseOutcome.DELIVERY_SCHEDULED, ""
        except CaseClosed as closed:
            outcome, note = CaseOutcome.CLOSED_BY_HUMAN, closed.note
        self._stage = outcome.value
        await log_event(inp.case.case_id, "case_finished", outcome=outcome, note=note)
        return CaseSummary(
            case_id=inp.case.case_id,
            outcome=outcome,
            sourcing=self._sourcing,
            order=self._order,
            fulfillment=self._fulfillment,
            patient_contacts=list(self._contacts),
            escalations=list(self._escalations),
            closing_note=note,
        )

    async def _work_case(self, inp: CaseInput) -> None:
        self._stage = "sourcing supplier + chasing written order"
        self._sourcing, self._order = await asyncio.gather(self._run_sourcing(inp, []), self._run_order(inp))
        await self._ensure_supplier(inp, [])
        await self._ensure_order(inp)

        self._stage = "updating patient on the plan"
        primary = self._sourcing.qualified[0]
        await self._contact_patient(
            inp, "plan", plan_points(primary.supplier.supplier_name, inp.case.pcp.name, primary.accepts_assignment)
        )

        await self._deliver(inp)
        scheduled = self._fulfillment.scheduled_with
        self._stage = "updating patient on delivery"
        await self._contact_patient(
            inp,
            "delivery",
            delivery_points(scheduled.supplier.supplier_name, self._fulfillment.delivery_eta_days, scheduled.accepts_assignment),
        )

    async def _ensure_supplier(self, inp: CaseInput, excluded_phones: list[str]) -> None:
        while not self._sourcing.qualified:
            await self._await_human(self._sourcing.escalation_id)
            self._sourcing = await self._run_sourcing(inp, excluded_phones)

    async def _ensure_order(self, inp: CaseInput) -> None:
        while self._order.status is not OrderOutcomeStatus.VALID:
            await self._await_human(self._order.escalation_id)
            self._order = await self._run_order(inp)

    async def _deliver(self, inp: CaseInput) -> None:
        tried: list[str] = []
        self._stage = "handing off to supplier"
        self._fulfillment = await self._run_fulfillment(inp)
        while self._fulfillment.scheduled_with is None:
            await self._await_human(self._fulfillment.escalation_id)
            tried = [*tried, *(a.phone for a in self._fulfillment.attempts)]
            self._stage = "re-sourcing without suppliers that dropped the case"
            self._sourcing = await self._run_sourcing(inp, tried)
            await self._ensure_supplier(inp, tried)
            self._stage = "handing off to supplier"
            self._fulfillment = await self._run_fulfillment(inp)

    async def _await_human(self, escalation_id: str | None) -> None:
        if escalation_id is None:
            raise CaseClosed("step failed without an escalation id; closing defensively")
        escalation = await workflow.execute_activity_method(
            CaseActivities.get_escalation,
            escalation_id,
            start_to_close_timeout=CASE_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY,
        )
        review_id = review_workflow_id(escalation_id)
        pending = PendingReview(
            escalation_id=escalation_id, review_workflow_id=review_id, reason=escalation.reason, context=escalation.context
        )
        self._pending_reviews = [*self._pending_reviews, pending]
        self._stage = f"WAITING FOR HUMAN: {escalation.reason}"
        workflow.set_current_details(f"⏸ Waiting for human review `{review_id}`: {escalation.reason}")

        decision = await workflow.execute_child_workflow(
            HumanReviewWorkflow.run,
            HumanReviewInput(case_id=self._case_id, escalation=escalation),
            id=review_id,
            static_summary=f"Human review: {escalation.reason}",
        )

        self._pending_reviews = [p for p in self._pending_reviews if p.escalation_id != escalation_id]
        workflow.set_current_details(f"Human decided **{decision.action}** on {escalation_id}")
        await workflow.execute_activity_method(
            CaseActivities.resolve_escalation,
            ResolveEscalation(escalation_id=escalation_id, case_id=self._case_id, action=decision.action, note=decision.note),
            start_to_close_timeout=CASE_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY,
        )
        if decision.action is HumanAction.CLOSE:
            raise CaseClosed(decision.note or "closed by care advocate")

    async def _contact_patient(self, inp: CaseInput, milestone: str, points: list[str]) -> None:
        try:
            contact = await self._patient.update(inp.case, milestone, points, inp.seconds_per_day)
        except ActivityError as error:
            category, detail = failure_category(error)
            escalation_id = await escalate(
                self._case_id,
                f"patient-update-failed-{milestone}",
                f"Patient update could not be placed ({category}); call her manually",
                blocking=False,
                error=detail,
            )
            contact = PatientContact(milestone=milestone, reached=False, escalation_id=escalation_id)
        self._contacts.append(contact)
        self._track(contact.escalation_id)

    async def _run_child(self, kind: str, run: Callable[[Any], Awaitable[StepResult]], arg: object) -> StepResult:
        """Business escalations come back inside results; technical failures (activity retries exhausted,
        non-retryable config errors) surface as ChildWorkflowError and become a blocking technical review."""
        while True:
            child_id = self._child_id(kind)
            try:
                result = await workflow.execute_child_workflow(run, arg, id=child_id)
            except ChildWorkflowError as error:
                category, detail = failure_category(error)
                escalation_id = await escalate(
                    self._case_id,
                    f"technical-{child_id.rsplit('/', 1)[-1]}",
                    f"Technical failure in {kind}: {category}",
                    step=kind,
                    error=detail,
                )
                self._track(escalation_id)
                await self._await_human(escalation_id)
                continue
            self._track(result.escalation_id)
            return result

    async def _run_sourcing(self, inp: CaseInput, excluded_phones: list[str]) -> SourcingResult:
        case = inp.case
        return await self._run_child(
            "sourcing",
            SupplierSourcingWorkflow.run,
            SourcingInput(
                case_id=case.case_id,
                equipment=case.equipment,
                patient_zip=case.patient.home_zip,
                seconds_per_day=inp.seconds_per_day,
                excluded_phones=excluded_phones,
            ),
        )

    async def _run_order(self, inp: CaseInput) -> OrderResult:
        case = inp.case
        return await self._run_child(
            "pcp-order",
            PcpOrderWorkflow.run,
            OrderInput(
                case_id=case.case_id,
                patient_name=case.patient.name,
                equipment=case.equipment,
                pcp=case.pcp,
                seconds_per_day=inp.seconds_per_day,
            ),
        )

    async def _run_fulfillment(self, inp: CaseInput) -> FulfillmentResult:
        case = inp.case
        return await self._run_child(
            "fulfillment",
            FulfillmentWorkflow.run,
            FulfillmentInput(
                case_id=case.case_id,
                patient_name=case.patient.name,
                patient_zip=case.patient.home_zip,
                equipment=case.equipment,
                order=self._order.order,
                candidates=self._sourcing.qualified,
                seconds_per_day=inp.seconds_per_day,
            ),
        )

    def _child_id(self, kind: str) -> str:
        run = self._child_runs.get(kind, 0) + 1
        self._child_runs[kind] = run
        suffix = kind if run == 1 else f"{kind}-{run}"
        return f"{workflow.info().workflow_id}/{suffix}"

    def _track(self, escalation_id: str | None) -> None:
        if escalation_id and escalation_id not in self._escalations:
            self._escalations.append(escalation_id)
