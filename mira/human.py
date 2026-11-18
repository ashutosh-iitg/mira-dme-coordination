"""Care-advocate console: see what needs a human and decide.

uv run python -m mira.human list
uv run python -m mira.human status <case-workflow-id>
uv run python -m mira.human decide <escalation-id> retry|close [--note "..."]   # blocking review
uv run python -m mira.human done <escalation-id> [--note "..."]                 # non-blocking callback
"""

import argparse
import asyncio
import sys

from temporalio.client import Client

from mira.config import ConfigError
from mira.models import HumanAction
from mira.escalation_store import EscalationStore, EscalationStoreError
from mira.reviews import InboxKind, ReviewNotFound, complete_callback, list_inbox, send_decision
from mira.temporal_client import connect
from mira.workflows.case import CaseWorkflow


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show blocking reviews (Temporal) and open callbacks")
    status = sub.add_parser("status", help="query a running case")
    status.add_argument("workflow_id")
    decide = sub.add_parser("decide", help="resolve a human review")
    decide.add_argument("escalation_id")
    decide.add_argument("action", choices=[a.value for a in HumanAction])
    decide.add_argument("--note", default="")
    done = sub.add_parser("done", help="mark a non-blocking callback as completed")
    done.add_argument("escalation_id")
    done.add_argument("--note", default="")
    return parser.parse_args()


async def _list(client: Client) -> None:
    inbox = await list_inbox(client, EscalationStore())
    if not inbox:
        print("Nothing needs a human right now.")
    for item in inbox:
        escalation = item.escalation
        label = "WAITING (case paused)" if item.kind is InboxKind.REVIEW else "CALLBACK (case continues)"
        action = "decide <id> retry|close" if item.kind is InboxKind.REVIEW else "done <id>"
        details = ", ".join(f"{k}={v}" for k, v in escalation.context.items() if k != "blocking")
        print(f"[{label}] {escalation.escalation_id}\n    {escalation.reason}\n    {details}\n    -> {action}")


async def _status(client: Client, workflow_id: str) -> None:
    status = await client.get_workflow_handle(workflow_id).query(CaseWorkflow.status)
    print(status.model_dump_json(indent=2))


async def _decide(client: Client, escalation_id: str, action: str, note: str) -> None:
    review_id = await send_decision(client, escalation_id, HumanAction(action), note)
    print(f"Sent {action} to {review_id}")


async def _run(args: argparse.Namespace) -> None:
    if args.command == "done":
        complete_callback(EscalationStore(), args.escalation_id, args.note)
        print(f"Callback {args.escalation_id} marked completed")
        return
    client = await connect()
    if args.command == "list":
        await _list(client)
    elif args.command == "status":
        await _status(client, args.workflow_id)
    else:
        await _decide(client, args.escalation_id, args.action, args.note)


def main() -> None:
    try:
        asyncio.run(_run(_parse_args()))
    except (ConfigError, ReviewNotFound, EscalationStoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
