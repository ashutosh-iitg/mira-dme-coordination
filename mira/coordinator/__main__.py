"""Drive a long-running coordinator via Temporal signals; JSON events stand in for inbound integrations."""

import argparse
import asyncio
import json
import sys
import uuid

from pydantic import ValidationError

from mira.config import ConfigError
from mira.coordinator.models import CoordinatorInput, FollowupTask, IncomingEvent, TaskKind
from mira.coordinator.store import CoordinatorError, JsonCaseStore, validate_event
from mira.coordinator.workflow import TASK_QUEUE, CaseCoordinatorWorkflow
from mira.data import load_case
from mira.escalation_store import EscalationStore
from mira.temporal_client import connect


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    for command in ("start", "add"):
        p = sub.add_parser(command)
        p.add_argument("case_id")
        p.add_argument("--task-id", required=True)
        p.add_argument("--kind", choices=list(TaskKind), required=True)
        p.add_argument("--goal", required=True)
        p.add_argument("--escalation", help="Attach an existing escalation as task provenance; does not resolve its human review")
        if command == "start":
            p.add_argument("--reminder-seconds", type=float, default=86400)
    p = sub.add_parser("event", help="Inject a typed inbound event (mock webhook)")
    p.add_argument("case_id")
    p.add_argument("--json", required=True, help="IncomingEvent JSON; event_id defaults to a fresh UUID")
    for command in ("status", "show", "close"):
        sub.add_parser(command).add_argument("case_id")
    return root


async def run(args: argparse.Namespace) -> None:
    store = JsonCaseStore()
    store._path(args.case_id)  # Validate before deriving a Temporal identifier.
    if args.command == "show":
        print(store.read(args.case_id).model_dump_json(indent=2))
        return
    client = await connect()
    handle = client.get_workflow_handle(f"coordinator-{args.case_id}")
    if args.command in {"start", "add"}:
        goal = args.goal
        if args.escalation:
            source = EscalationStore().get_escalation(args.escalation)
            if source is None:
                raise CoordinatorError("Source escalation not found")
            goal += "\nSource: " + source.model_dump_json()
        task = FollowupTask(
            task_id=args.task_id, kind=TaskKind(args.kind), goal=goal,
            source_escalation_id=args.escalation,
        )
        if args.command == "start":
            case = load_case()
            handle = await client.start_workflow(
                CaseCoordinatorWorkflow.run,
                CoordinatorInput(case_id=args.case_id, patient_name=case.patient.name,
                                 tasks=[task], reminder_seconds=args.reminder_seconds),
                id=handle.id, task_queue=TASK_QUEUE,
                static_summary=f"Agent coordinator: {args.case_id}",
            )
        else:
            await handle.signal(CaseCoordinatorWorkflow.add_task, task)
        print(f"Submitted {task.task_id} to {handle.id}")
    elif args.command == "event":
        payload = json.loads(args.json)
        payload.setdefault("event_id", str(uuid.uuid4()))
        event = IncomingEvent.model_validate(payload)
        current = await handle.query(CaseCoordinatorWorkflow.status)
        task = next((t for t in current.tasks if t.task_id == event.task_id), None) if current else None
        if task is None:
            raise CoordinatorError("Task not found; wait for coordinator initialization or check task_id")
        validate_event(event, task.kind)
        await handle.signal(CaseCoordinatorWorkflow.receive, event)
        print(f"Submitted event {event.event_id}")
    elif args.command == "status":
        state = await handle.query(CaseCoordinatorWorkflow.status)
        print(state.model_dump_json(indent=2) if state else "Initializing")
    else:
        await handle.signal(CaseCoordinatorWorkflow.close)
        print(f"Close requested for {handle.id}")


def main() -> None:
    try:
        asyncio.run(run(parser().parse_args()))
    except (ConfigError, CoordinatorError, ValidationError, json.JSONDecodeError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
