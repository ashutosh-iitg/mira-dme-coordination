"""Sequential live-agent evaluation. External parties and time advancement remain scripted.

Runs the production planner, action validation and JSON state lifecycle directly; does not
start Cloud workflows. Temporal durability is covered separately by workflow tests.
"""

import argparse
import asyncio
import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from langchain_core.callbacks import AsyncCallbackHandler
from openai import RateLimitError

from mira.config import DATA_DIR, RUNTIME_DIR, llm_settings
from mira.coordinator.activities import CoordinatorActivities
from mira.coordinator.agent import coordinator_model, propose_next_action
from mira.coordinator.models import (
    CoordinatorInput, FollowupTask, IncomingEvent, SyncRequest, TaskKind, TurnRequest,
)
from mira.coordinator.store import JsonCaseStore

SCENARIOS_PATH = DATA_DIR / "coordinator_eval" / "scenarios.json"


def duration_seconds(value: str) -> float:
    return sum(float(number) * {"h": 3600, "m": 60, "s": 1, "ms": .001}[unit]
               for number, unit in re.findall(r"([\d.]+)(ms|h|m|s)", value))


class Pacing(AsyncCallbackHandler):
    """Serialize model requests, honor Groq's refill/reset headers, record usage."""
    raise_error = True

    def __init__(self, minimum_seconds: float = 20) -> None:
        self.minimum_seconds = minimum_seconds
        self.next_request = 0.0
        self.calls = 0
        self.rate_limits = 0
        self.input_tokens = 0
        self.output_tokens = 0

    async def on_chat_model_start(self, serialized, messages, **kwargs):
        remaining = self.next_request - time.monotonic()
        if remaining > 0:
            print(f"  pacing: waiting {remaining:.1f}s before next model request", flush=True)
        while (remaining := self.next_request - time.monotonic()) > 0:
            await asyncio.sleep(min(remaining, 10))
        self.calls += 1
        self.next_request = time.monotonic() + self.minimum_seconds

    async def on_llm_end(self, response, **kwargs):
        message = response.generations[0][0].message
        usage = message.usage_metadata or {}
        self.input_tokens += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)
        headers = message.response_metadata.get("headers", {})
        # Refill the token bucket before another large agent request. Exact limits
        # come from this account's response headers, not an assumed plan quota.
        reset = duration_seconds(headers.get("x-ratelimit-reset-tokens", ""))
        self.next_request = max(self.next_request, time.monotonic() + reset + 1)

    async def on_llm_error(self, error, **kwargs):
        if isinstance(error, RateLimitError):
            self.rate_limits += 1
            headers = error.response.headers
            delay = max(float(headers.get("retry-after", 60)), 20)
            self.next_request = max(self.next_request, time.monotonic() + delay + 1)


def scenario_events(stage: dict, now: datetime) -> list[IncomingEvent]:
    substitutions = {
        "$slot_start": (now + timedelta(days=3)).isoformat(),
        "$offer_expiry": (now + timedelta(days=1)).isoformat(),
    }
    return [IncomingEvent.model_validate({
        "task_id": "followup",
        **{key: substitutions.get(value, value) if isinstance(value, str) else value for key, value in event.items()},
    }) for event in stage.get("events", [])]


async def evaluate(output: Path, minimum_seconds: float = 20) -> dict:
    scenarios = json.loads(SCENARIOS_PATH.read_text())
    output.mkdir(parents=True, exist_ok=False)
    settings = llm_settings()
    pacer = Pacing(minimum_seconds)
    model = coordinator_model().model_copy(update={
        "callbacks": [pacer], "include_response_headers": True, "max_retries": 0,
    })

    async def planner(doc):
        for attempt in range(3):
            try:
                return await propose_next_action(doc, model=model)
            except RateLimitError:
                if attempt == 2:
                    raise
                print("  rate limited: retaining state and backing off before retry", flush=True)
        raise AssertionError("unreachable")

    started = datetime.now(UTC)
    report = {
        "provider": settings.provider, "model": model.model_name,
        "started_at": started.isoformat(), "mode": "live model; scripted parties; direct production activity path",
        "scenarios": [],
    }
    for scenario in scenarios:  # Deliberately sequential; no concurrent model calls.
        print(f"SCENARIO {scenario['id']}: {scenario['title']}", flush=True)
        store = JsonCaseStore(output / "cases")
        inp = CoordinatorInput(case_id=scenario["id"], patient_name="Demo User", tasks=[
            FollowupTask(task_id="followup", kind=TaskKind(scenario["kind"]), goal=scenario["goal"]),
        ])
        activities = CoordinatorActivities(store, planner)
        now = started
        await activities.sync(SyncRequest(inp=inp, now=now))
        result = {"id": scenario["id"], "title": scenario["title"], "pain": scenario["pain"], "stages": []}
        for index, stage in enumerate(scenario["stages"]):
            now += timedelta(hours=stage.get("advance_hours", 0))
            # Re-open the JSON adapter each turn to test context reconstruction.
            activities = CoordinatorActivities(JsonCaseStore(output / "cases"), planner)
            await activities.sync(SyncRequest(inp=inp, now=now, events=scenario_events(stage, now)))
            task = store.read(inp.case_id).tasks["followup"]
            item = {"name": stage["name"], "simulated_at": now.isoformat(), "expected_kinds": stage["expected_kinds"]}
            if task.status.value != "ready":
                item.update(passed=False, error=f"Task is {task.status}; model was not invoked")
            else:
                print(f"  turn {index + 1}: {stage['name']}", flush=True)
                request = TurnRequest(case_id=inp.case_id, turn_id=f"{inp.case_id}-turn-{index + 1}", now=now, reminder_seconds=86400)
                began = time.monotonic()
                try:
                    decision = await activities.plan(request)
                    state = await activities.apply(decision)
                    committed = store.read(inp.case_id).actions[-1]
                    expected_kind = decision.action.kind.value in stage["expected_kinds"]
                    expected_party = not stage.get("expected_parties") or decision.action.party in stage["expected_parties"]
                    gate_accepted = committed["kind"] == decision.action.kind.value and not committed.get("not_sent")
                    item.update(
                        passed=bool(expected_kind and expected_party and gate_accepted),
                        proposed=decision.action.model_dump(mode="json"), committed=committed,
                        final_status=state.tasks[0].status.value, gate_accepted=gate_accepted,
                        elapsed_seconds=round(time.monotonic() - began, 2),
                    )
                    print(f"  -> {decision.action.kind} / {decision.action.party}; status={state.tasks[0].status}; passed={item['passed']}", flush=True)
                except Exception as exc:
                    # Preserve failures, do not silently repair prompts or change the expected outcome.
                    item.update(passed=False, error=f"{type(exc).__name__}: {exc}")
                    print(f"  -> ERROR {item['error']}", flush=True)
            result["stages"].append(item)
            (output / "report.json").write_text(json.dumps({**report, "in_progress": result}, indent=2))
        doc = store.read(inp.case_id)
        result.update(
            expected_final_status=scenario["expected_final_status"], final_status=doc.tasks["followup"].status.value,
            final_state_passed=doc.tasks["followup"].status.value == scenario["expected_final_status"],
        )
        result["passed"] = result["final_state_passed"] and all(s["passed"] for s in result["stages"])
        report["scenarios"].append(result)
        (output / "report.json").write_text(json.dumps(report, indent=2))
    report.update(
        finished_at=datetime.now(UTC).isoformat(), model_requests=pacer.calls,
        rate_limit_responses=pacer.rate_limits, input_tokens=pacer.input_tokens, output_tokens=pacer.output_tokens,
        scenarios_passed=sum(s["passed"] for s in report["scenarios"]),
        stages_passed=sum(t["passed"] for s in report["scenarios"] for t in s["stages"]),
        stages_total=sum(len(s["stages"]) for s in report["scenarios"]),
    )
    (output / "report.json").write_text(json.dumps(report, indent=2))
    print(f"RESULT: {report['scenarios_passed']}/{len(scenarios)} scenarios; {report['stages_passed']}/{report['stages_total']} stages; {pacer.rate_limits} rate-limit responses", flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=RUNTIME_DIR / "coordinator_eval" / datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--minimum-seconds", type=float, default=20, help="Minimum spacing between model requests; token-reset headers may extend this")
    args = parser.parse_args()
    if args.minimum_seconds < 0:
        parser.error("--minimum-seconds must be nonnegative")
    asyncio.run(evaluate(args.output, args.minimum_seconds))


if __name__ == "__main__":
    main()
