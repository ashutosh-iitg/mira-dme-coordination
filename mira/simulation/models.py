from datetime import datetime

from pydantic import Field

from mira.coordinator.models import CoordinatorInput, Snapshot
from mira.models import Case, CaseSummary, Frozen


class SimulationInput(Frozen):
    scenario: dict
    case: Case
    coordinator: CoordinatorInput


class SimulationEvent(Frozen):
    case_id: str
    business_time: datetime
    kind: str
    details: dict = Field(default_factory=dict)


class StageRequest(Frozen):
    inp: SimulationInput
    stage_index: int
    business_time: datetime


class ActionRequest(Frozen):
    case_id: str
    business_time: datetime
    snapshot: Snapshot


class SimulationResult(Frozen):
    scenario_id: str
    outcome: str
    coordinator: Snapshot
    stage_checks: list[dict]
    dme: CaseSummary | None = None
    workflow_ids: list[str]
    passed: bool
