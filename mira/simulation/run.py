"""Run the five scenarios sequentially through real Temporal workflows and mock voice calls."""

import argparse
import asyncio
import html
import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openai import RateLimitError
from temporalio import activity
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from mira.activities import CallActivities
from mira.case_activities import CaseActivities
from mira.config import RUNTIME_DIR, VOICE_CALL_TASK_QUEUE, llm_settings
from mira.coordinator.activities import CoordinatorActivities, Planner
from mira.coordinator.agent import coordinator_model, propose_next_action
from mira.coordinator.evaluate import Pacing, SCENARIOS_PATH
from mira.coordinator.models import CoordinatorInput, FollowupTask, TaskKind
from mira.coordinator.store import JsonCaseStore
from mira.coordinator.workflow import CaseCoordinatorWorkflow
from mira.escalation_store import EscalationStore
from mira.extraction import StructuredCompleter
from mira.llm import _parse
from mira.mock_fax import MockFaxInbox
from mira.simulation.activities import SimulationActivities, SimulationTrace, TracedVoiceAgent, delivery_scenario, demo_case
from mira.simulation.models import SimulationInput, SimulationResult
from mira.simulation.workflow import SimulatedDmeCaseWorkflow
from mira.worker import WORKFLOWS, case_activity_methods

TASK_QUEUE = "dme-e2e-simulation"


def render_trace(folder: Path, result: dict) -> None:
    events = [json.loads(line) for line in (folder / "trace.jsonl").read_text().splitlines()]
    def display(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S IST") if value else "not recorded"

    lines = [f"# {result['scenario_id']} — end-to-end trace", "",
        f"Outcome: **{result.get('outcome', 'failed')}**. Expected-decision checks: **{'PASS' if result.get('passed') else 'FAIL'}**.", "",
        "Displayed times are IST (Asia/Kolkata); raw JSON and histories retain UTC timestamps. Actual time is wall-clock execution time. Business time is Temporal workflow time (or the activity's scheduled time); long waits are accelerated by the test server. No real calls, bookings, claims or equipment delivery occur.", "",
        "Workflow histories are exported in `histories/`. `result.json` contains the final DME/coordinator results.", ""]
    for index, event in enumerate(events, 1):
        lines += [f"## {index}. {event['kind']}", "",
            f"Business: `{display(event.get('business_time_utc'))}` · Actual: `{display(event['actual_time_utc'])}`", "",
            f"Workflow: `{event.get('workflow_id')}`", "", "```json",
            json.dumps({k: v for k, v in event.items() if k not in {'actual_time_utc', 'business_time_utc', 'workflow_id'}}, indent=2),
            "```", ""]
    retries = []
    for path in (folder / "histories").glob("*.json"):
        for event in json.loads(path.read_text()).get("events", []):
            started = event.get("activityTaskStartedEventAttributes", {})
            if int(started.get("attempt", 1)) > 1:
                retries.append({"history": path.name, "business_time_utc": event["eventTime"], **started})
    if retries:
        lines += ["## Activity retries recorded by Temporal", "",
            "Intermediate activity failures can be compacted by Temporal. The final started event retains its attempt number; inspect the worker trace for the original exception when recorded.", "",
            "```json", json.dumps(retries, indent=2), "```", ""]
    (folder / "trace.md").write_text("\n".join(lines))
    cards = []
    for event in events:
        kind = event["kind"]
        category = "calls" if "call" in kind else "agent" if "agent" in kind or "coordinator" in kind else "timers" if "timer" in kind else "human" if "human" in kind or "review" in kind else "workflow"
        body = json.dumps({k: v for k, v in event.items() if k not in {"actual_time_utc", "business_time_utc"}}, indent=2)
        cards.append(f'<details class="event" data-category="{category}"><summary><b>{html.escape(kind)}</b>'
            f'<span>Business {html.escape(display(event.get("business_time_utc")))}</span>'
            f'<span>Executed {html.escape(display(event["actual_time_utc"]))}</span></summary>'
            f'<pre>{html.escape(body)}</pre></details>')
    if retries:
        cards.append('<details class="event" data-category="retries"><summary><b>Temporal activity retries</b></summary><pre>'
                     + html.escape(json.dumps(retries, indent=2)) + '</pre></details>')
    document = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(result['scenario_id'])} — Temporal trace</title>
<style>body{{font:15px system-ui;margin:32px auto;padding:0 20px;max-width:1100px;color:#172d35;background:#f4f7f7}}h1{{font-size:28px}}.event{{margin:12px 0;border:1px solid #cbdada;border-left:5px solid #247b81;border-radius:6px;background:white}}summary{{cursor:pointer;padding:14px}}summary span{{display:block;font-size:12px;color:#52656d;margin:5px 0 0 16px}}pre{{overflow:auto;padding:16px;background:#edf3f3;white-space:pre-wrap;overflow-wrap:anywhere}}select{{padding:8px}}a{{color:#12656d}}</style>
<h1>{html.escape(result['scenario_id'])}</h1><p>Outcome: <b>{html.escape(result.get('outcome','failed'))}</b> · Expected-decision checks: <b>{'PASS' if result.get('passed') else 'FAIL'}</b></p>
<p>Real local Temporal workflows and live model decisions. Voice, fax, booking and human actors are mocked. Business time is accelerated; execution time is wall-clock. Displayed times use IST; raw artifacts use UTC.</p>
<p><a href="result.json">Final result</a> · <a href="trace.jsonl">Raw events</a> · <a href="trace.md">Markdown</a> · <a href="histories/">Temporal histories</a></p>
<label>Show <select id="filter"><option value="all">All events</option><option>calls</option><option>agent</option><option>timers</option><option>human</option><option>workflow</option><option>retries</option></select></label>
{''.join(cards)}
<script>document.getElementById('filter').onchange=function(){{document.querySelectorAll('.event').forEach(e=>e.hidden=this.value!=='all'&&e.dataset.category!==this.value)}};</script></html>'''
    (folder / "trace.html").write_text(document)


async def export_histories(client: Client, workflow_ids: list[str], folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for workflow_id in workflow_ids:
        try:
            history = await client.get_workflow_handle(workflow_id).fetch_history()
            (folder / (workflow_id.replace("/", "__") + ".json")).write_text(history.to_json())
        except Exception as exc:
            # A failed parent may never have started its later children; preserve the omission.
            (folder / (workflow_id.replace("/", "__") + ".missing.txt")).write_text(str(exc))


def render_index(output: Path, report: dict) -> None:
    rows = []
    for result in report["scenarios"]:
        name = result["scenario_id"]
        events = [json.loads(line) for line in (output / name / "trace.jsonl").read_text().splitlines()]
        calls = sum(e["kind"] == "mock_call_completed" for e in events)
        histories = len(list((output / name / "histories").glob("*.json")))
        rows.append(f'<tr><td><a href="{html.escape(name)}/trace.html">{html.escape(name)}</a></td>'
                    f'<td>{html.escape(result["outcome"])}</td><td>{"Pass" if result["passed"] else "Fail"}</td>'
                    f'<td>{calls}</td><td>{histories}</td></tr>')
    page = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Temporal end-to-end case traces</title><style>body{{font:16px system-ui;margin:40px auto;max-width:1100px;padding:0 20px;color:#17353d;background:#f4f7f7}}table{{width:100%;border-collapse:collapse;background:white}}th,td{{padding:14px;text-align:left;border-bottom:1px solid #d1dddd}}a{{color:#12656d}}code{{background:#e6eeee;padding:3px}}</style>
<h1>Temporal end-to-end case traces</h1><p>Groq <code>{html.escape(report['model'])}</code> · {report.get('passed',0)}/5 scenario checks passed · {report.get('rate_limit_responses',0)} rate-limit responses</p>
<p>Each case runs through a parent Temporal workflow, a coordinator child, mocked calls and delayed callbacks. Completed administrative prerequisites unblock the existing DME workflows. Clinical or unresolved cases go to human review.</p>
<table><thead><tr><th>Scenario / timeline</th><th>Case outcome</th><th>Expected decisions</th><th>Mock calls</th><th>Histories</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<p>The model and local Temporal executions are real. Voice, fax, booking, external confirmations and human responses are scripted. Delivery scheduled does not mean physical delivery. Timelines show actual execution and accelerated business time.</p>
<p><a href="report.json">Complete JSON report</a> · <a href="index.md">Markdown index</a></p></html>'''
    (output / "index.html").write_text(page)


async def run_one(scenario: dict, folder: Path, planner: Planner, complete: StructuredCompleter) -> dict:
    folder.mkdir(parents=True, exist_ok=False)
    trace = SimulationTrace(folder / "trace.jsonl")
    store = JsonCaseStore(folder / "state")
    case = demo_case(scenario["id"])
    coordinator = CoordinatorActivities(store, planner)
    simulation = SimulationActivities(store, trace)

    class FaxInbox(MockFaxInbox):
        def fetch(self, attempt):
            from temporalio import activity
            order = super().fetch(attempt)
            return order.model_copy(update={"order_date": activity.info().scheduled_time.date()}) if order else None

    async with await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env:
        voice_scenario = delivery_scenario(case, await env.get_current_time())
        calls = CallActivities(TracedVoiceAgent(voice_scenario, trace), trace, complete=complete)
        cases = CaseActivities(trace, FaxInbox(voice_scenario), store=EscalationStore(folder / "escalations.json"))
        task = FollowupTask(task_id="followup", kind=TaskKind(scenario["kind"]), goal=scenario["goal"])
        inp = SimulationInput(scenario=scenario, case=case, coordinator=CoordinatorInput(
            case_id=case.case_id, patient_name=case.patient.name, tasks=[task], reminder_seconds=8 * 86400,
        ))
        activities = [
            simulation.prepare, simulation.record, simulation.deliver_stage, simulation.dispatch_action,
            coordinator.sync, coordinator.plan, coordinator.apply, coordinator.failure, *case_activity_methods(cases),
        ]
        root_id = f"e2e-{folder.parent.name}-{scenario['id']}"
        async with (
            Worker(env.client, task_queue=TASK_QUEUE, workflows=[SimulatedDmeCaseWorkflow, CaseCoordinatorWorkflow, *WORKFLOWS], activities=activities),
            Worker(env.client, task_queue=VOICE_CALL_TASK_QUEUE, activities=[calls.place_call], max_concurrent_activities=1),
        ):
            try:
                result = await env.client.execute_workflow(
                    SimulatedDmeCaseWorkflow.run, inp, id=root_id, task_queue=TASK_QUEUE,
                )
                data = result.model_dump(mode="json")
            except Exception as exc:
                data = {"scenario_id": scenario["id"], "outcome": "workflow_failed", "passed": False,
                        "error": f"{type(exc).__name__}: {exc}", "workflow_ids": [root_id]}
                trace.append(case.case_id, "workflow_failed", {"error": data["error"]})
            await export_histories(env.client, data["workflow_ids"], folder / "histories")
    (folder / "result.json").write_text(json.dumps(data, indent=2))
    render_trace(folder, data)
    print(f"FINISHED {scenario['id']}: {data['outcome']}; passed={data['passed']}", flush=True)
    return data


async def run(output: Path, minimum_seconds: float = 20) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    pacing = Pacing(minimum_seconds)
    model = coordinator_model().model_copy(update={"callbacks": [pacing], "include_response_headers": True, "max_retries": 0})
    settings = llm_settings()
    report = {"mode": "real local Temporal time-skipping server; live coordinator and extraction; mocked voice/fax/bookings/human",
              "provider": settings.provider, "model": model.model_name,
              "started_at": datetime.now(UTC).isoformat(), "scenarios": []}

    async def with_backoff(call):
        for attempt in range(3):
            try:
                return await call()
            except RateLimitError:
                if attempt == 2:
                    raise
                print("Rate limited: retaining state and waiting for refill", flush=True)

    for scenario in json.loads(SCENARIOS_PATH.read_text()):
        print(f"STARTING {scenario['id']}", flush=True)
        trace = SimulationTrace(output / scenario["id"] / "trace.jsonl")

        async def planner(doc):
            trace.append(doc.case_id, "agent_turn_started", {"version": doc.version, "attempt": activity.info().attempt})
            try:
                action = await with_backoff(lambda: propose_next_action(doc, model=model))
            except Exception as exc:
                trace.append(doc.case_id, "agent_turn_failed", {"attempt": activity.info().attempt,
                    "error": f"{type(exc).__name__}: {exc}"})
                raise
            trace.append(doc.case_id, "agent_proposed_action", {"action": action.model_dump(mode="json")})
            return action

        async def complete(system_prompt, user_prompt, schema):
            response = await with_backoff(lambda: model.ainvoke([
                ("system", system_prompt + "\nJSON schema:\n" + json.dumps(schema.model_json_schema())),
                ("user", user_prompt),
            ], response_format={"type": "json_object"}))
            return _parse(response.content, schema)

        result = await run_one(scenario, output / scenario["id"], planner, complete)
        report["scenarios"].append(result)
        (output / "report.json").write_text(json.dumps(report, indent=2))
    report.update(finished_at=datetime.now(UTC).isoformat(), model_requests=pacing.calls,
                  rate_limit_responses=pacing.rate_limits, input_tokens=pacing.input_tokens, output_tokens=pacing.output_tokens,
                  passed=sum(s["passed"] for s in report["scenarios"]))
    (output / "report.json").write_text(json.dumps(report, indent=2))
    lines = ["# End-to-end Temporal simulations", "", f"Model: `{model.model_name}`. Passed: {report['passed']}/5. Rate-limit responses: {pacing.rate_limits}.", "",
             "| Scenario | Outcome | Checks | Trace |", "|---|---|---|---|"]
    for result in report["scenarios"]:
        name = result["scenario_id"]
        lines.append(f"| {name} | {result['outcome']} | {'Pass' if result['passed'] else 'Fail'} | [Timeline]({name}/trace.md) |")
    (output / "index.md").write_text("\n".join(lines) + "\n")
    render_index(output, report)
    print(f"COMPLETE: {report['passed']}/5; traces: {output / 'index.md'}", flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=RUNTIME_DIR / "e2e" / datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--minimum-seconds", type=float, default=20)
    args = parser.parse_args()
    if args.minimum_seconds < 0:
        parser.error("--minimum-seconds must be nonnegative")
    asyncio.run(run(args.output, args.minimum_seconds))


if __name__ == "__main__":
    main()
