import json
from types import SimpleNamespace

from mira.coordinator import evaluate as evaluation
from mira.coordinator.models import ActionKind, ProposedAction


def test_rate_reset_duration_parser_handles_compound_units():
    assert evaluation.duration_seconds("1m2.5s") == 62.5
    assert evaluation.duration_seconds("500ms") == .5
    assert evaluation.duration_seconds("") == 0


async def test_final_completion_does_not_hide_a_failed_intermediate_decision(tmp_path, monkeypatch):
    scenario = json.loads(evaluation.SCENARIOS_PATH.read_text())[0]
    scenario["stages"] = [scenario["stages"][0], scenario["stages"][-1]]
    fixtures = tmp_path / "scenarios.json"
    fixtures.write_text(json.dumps([scenario]))
    monkeypatch.setattr(evaluation, "SCENARIOS_PATH", fixtures)
    monkeypatch.setattr(evaluation, "llm_settings", lambda: SimpleNamespace(provider="offline-test"))

    class Model:
        model_name = "offline-test"

        def model_copy(self, update):
            return self

    monkeypatch.setattr(evaluation, "coordinator_model", Model)

    async def planner(doc, model):
        if doc.events:
            return ProposedAction(task_id="followup", kind=ActionKind.COMPLETE,
                                  reason="PCP accepted report", evidence_event_id="pt-report")
        return ProposedAction(task_id="followup", kind=ActionKind.WAIT,
                              reason="Intentionally wrong first decision for evaluator test")

    monkeypatch.setattr(evaluation, "propose_next_action", planner)
    report = await evaluation.evaluate(tmp_path / "run", minimum_seconds=0)

    result = report["scenarios"][0]
    assert result["final_state_passed"]
    assert result["passed"] is False
    assert report["stages_passed"] == 1
    assert report["scenarios_passed"] == 0
    assert json.loads((tmp_path / "run" / "report.json").read_text())["scenarios_passed"] == 0
