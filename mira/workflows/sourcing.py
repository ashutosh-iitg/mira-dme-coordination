from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from mira.case_activities import CaseActivities
    from mira.models import (
        CallPurpose,
        CallRequest,
        RankedSupplier,
        RankRequest,
        SourcingInput,
        SourcingResult,
        Supplier,
        SupplierStatus,
        SupplierVerdict,
        RequirementResolution,
    )
    from mira.policy import ScreenDecision, assess_supplier_call, order_candidates, requirement_to_coordinate
    from mira.policy import MIN_EXTRACTION_CONFIDENCE
    from mira.workflows.requirement_gate import RequirementGate, supplier_contact
    from mira.workflows.common import (
        CASE_ACTIVITY_TIMEOUT,
        DEFAULT_RETRY,
        call_id,
        escalate,
        log_event,
        place_call,
        sleep_days,
    )

def _screen_talking_points(inp: SourcingInput) -> list[str]:
    return [
        "Patient has Original Medicare Part B, no supplemental plan",
        f"Needs {inp.equipment.description} (HCPCS {inp.equipment.hcpcs_code})",
        f"Delivery ZIP {inp.patient_zip}",
        "Ask: taking new Medicare patients? item in stock? deliver to that ZIP, how soon? accept Medicare assignment? will you take the case?",
    ]


@workflow.defn
class SupplierSourcingWorkflow:
    """Works the supplier directory by phone until it has a primary and a backup supplier."""

    def __init__(self) -> None:
        self._verdicts: dict[str, SupplierVerdict] = {}
        self._requirements = RequirementGate()
        self._blocked_escalation_id: str | None = None

    @workflow.signal
    def requirement_resolved(self, result: RequirementResolution) -> None:
        self._requirements.resolve(result)

    @workflow.query
    def verdicts(self) -> list[SupplierVerdict]:
        return list(self._verdicts.values())

    @workflow.run
    async def run(self, inp: SourcingInput) -> SourcingResult:
        ranked: list[RankedSupplier] = await workflow.execute_activity_method(
            CaseActivities.rank_suppliers,
            RankRequest(patient_zip=inp.patient_zip, excluded_phones=inp.excluded_phones),
            start_to_close_timeout=CASE_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY,
        )
        distances = {r.supplier.phone: r.distance_miles for r in ranked}
        pending = [r.supplier for r in ranked]
        attempts: dict[str, int] = {}
        qualified: list[SupplierVerdict] = []
        round_number = 0

        while pending and len(qualified) < inp.wanted_candidates:
            if round_number > 0:
                await log_event(inp.case_id, "sourcing_callback_round_scheduled", round=round_number + 1, suppliers=len(pending))
                await sleep_days(inp.callback_delay_days, inp.seconds_per_day)
            round_number += 1
            call_back_later: list[Supplier] = []

            for supplier in pending:
                if len(qualified) >= inp.wanted_candidates:
                    break
                attempt = attempts.get(supplier.phone, 0) + 1
                attempts[supplier.phone] = attempt
                verdict = await self._screen(inp, supplier, attempt, distances[supplier.phone])
                if self._blocked_escalation_id:
                    return SourcingResult(qualified=[], others=list(self._verdicts.values()), escalation_id=self._blocked_escalation_id)
                if verdict.status is SupplierStatus.QUALIFIED:
                    qualified.append(verdict)
                elif verdict.status is SupplierStatus.UNREACHABLE and attempt < inp.max_attempts_per_supplier:
                    call_back_later.append(supplier)

            pending = call_back_later

        return await self._finish(inp, qualified)

    async def _screen(self, inp: SourcingInput, supplier: Supplier, attempt: int, distance: float | None) -> SupplierVerdict:
        request = CallRequest(
            case_id=inp.case_id,
            call_id=call_id(inp.case_id, CallPurpose.SUPPLIER_SCREEN, supplier.phone, attempt),
            purpose=CallPurpose.SUPPLIER_SCREEN,
            party_name=supplier.supplier_name,
            phone=supplier.phone,
            attempt=attempt,
            talking_points=_screen_talking_points(inp),
        )
        outcome = await place_call(request)
        for round_number in range(3):
            requirement = requirement_to_coordinate(outcome.supplier.information_request if outcome.supplier else None)
            if not (inp.coordinator_enabled and requirement and outcome.confidence >= MIN_EXTRACTION_CONFIDENCE):
                break
            result = await self._requirements.wait(inp.case_id, supplier_contact(supplier), request.call_id, requirement, inp.seconds_per_day)
            if not result.cleared:
                self._blocked_escalation_id = await escalate(inp.case_id, "supplier-information-review",
                    "Supplier information requirement needs human review", task_id=result.task_id, reason_detail=result.reason)
                break
            # Acceptance clears only this requirement. Recheck qualification against the supplier's current response.
            request = request.model_copy(update={"call_id": f"{request.call_id}:recheck-{round_number + 1}",
                "attempt": request.attempt + 1,
                "talking_points": [*_screen_talking_points(inp), f"You accepted {result.accepted_reference}; re-confirm the case can proceed"]})
            outcome = await place_call(request)
        if (inp.coordinator_enabled and outcome.supplier and requirement_to_coordinate(outcome.supplier.information_request)
            and not self._blocked_escalation_id):
            self._blocked_escalation_id = await escalate(inp.case_id, "supplier-information-budget",
                "Supplier requirement remains unresolved after bounded clarification rounds")
        assessment = assess_supplier_call(outcome, inp.equipment.hcpcs_code)
        status = {
            ScreenDecision.QUALIFIED: SupplierStatus.QUALIFIED,
            ScreenDecision.REJECTED: SupplierStatus.REJECTED,
            ScreenDecision.RETRY: SupplierStatus.UNREACHABLE,
        }[assessment.decision]
        verdict = SupplierVerdict(
            supplier=supplier,
            distance_miles=distance,
            status=status,
            reason=assessment.reason,
            attempts=request.attempt,
            delivery_eta_days=outcome.supplier.delivery_eta_days if outcome.supplier else None,
            accepts_assignment=outcome.supplier.accepts_assignment if outcome.supplier else None,
            call_id=request.call_id,
        )
        self._verdicts[supplier.phone] = verdict
        await log_event(inp.case_id, "supplier_screened", supplier=supplier.supplier_name, status=status, reason=assessment.reason)
        return verdict

    async def _finish(self, inp: SourcingInput, qualified: list[SupplierVerdict]) -> SourcingResult:
        ordered = order_candidates(qualified)
        others = [v for v in self._verdicts.values() if v.status is not SupplierStatus.QUALIFIED]
        escalation_id = None
        if not ordered:
            escalation_id = await escalate(
                inp.case_id,
                "no-supplier",
                "No enrolled supplier could be confirmed for this patient",
                rejected=sum(v.status is SupplierStatus.REJECTED for v in others),
                unreachable=sum(v.status is SupplierStatus.UNREACHABLE for v in others),
            )
        await log_event(
            inp.case_id,
            "sourcing_finished",
            qualified=", ".join(v.supplier.supplier_name for v in ordered) or "none",
        )
        return SourcingResult(qualified=ordered, others=others, escalation_id=escalation_id)
