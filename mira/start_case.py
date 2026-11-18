"""Start Eleanor's case on Temporal Cloud, follow it live, and decide human reviews inline.

uv run python -m mira.start_case               # prompts when the case waits for a human
uv run python -m mira.start_case --no-prompt   # leave decisions to `mira.human`
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from urllib.parse import quote

from temporalio.client import Client, WorkflowHandle
from temporalio.service import RPCError

from mira.config import CASE_TASK_QUEUE, ConfigError, seconds_per_day
from mira.data import DataError, load_case
from mira.models import (
    CaseInput,
    CaseStatus,
    CaseSummary,
    FulfillmentResult,
    HumanAction,
    OrderResult,
    PendingReview,
    SourcingResult,
)
from mira.reviews import ReviewNotFound, send_decision
from mira.temporal_client import connect
from mira.workflows.case import CaseWorkflow

TEMPORAL_UI = "https://cloud.temporal.io/namespaces/{namespace}/workflows/{workflow_id}"
STATUS_POLL_SECONDS = 2.0
BANNER = "=" * 72
PROMPT_CHOICES = {"r": HumanAction.RETRY, "retry": HumanAction.RETRY, "c": HumanAction.CLOSE, "close": HumanAction.CLOSE}


def _ui_link(namespace: str, workflow_id: str) -> str:
    return TEMPORAL_UI.format(namespace=namespace, workflow_id=quote(workflow_id, safe=""))


def _print_sourcing(sourcing: SourcingResult) -> None:
    print("Supplier sourcing:")
    for rank, verdict in enumerate(sourcing.qualified, start=1):
        label = "primary" if rank == 1 else "backup"
        eta = f"{verdict.delivery_eta_days}d" if verdict.delivery_eta_days is not None else "unknown"
        print(f"  {label}: {verdict.supplier.supplier_name} (ETA {eta}, {verdict.distance_miles} mi)")
    for verdict in sourcing.others:
        print(f"  - {verdict.supplier.supplier_name}: {verdict.status} ({verdict.reason}, {verdict.attempts} calls)")


def _print_order(order: OrderResult) -> None:
    print(f"Written order: {order.status} after {order.nudges} nudges")
    if order.order:
        print(f"  {order.order.hcpcs_code} {order.order.item_description}, signed by {order.order.prescriber_name}")
    for problem in order.problems_seen:
        print(f"  caught and fixed: {problem}")


def _print_fulfillment(fulfillment: FulfillmentResult) -> None:
    print("Handoff:")
    for attempt in fulfillment.attempts:
        print(f"  - {attempt.supplier_name}: {attempt.result} after {attempt.followup_calls} follow-up calls")
    if fulfillment.scheduled_with:
        print(f"  delivery scheduled with {fulfillment.scheduled_with.supplier.supplier_name} in ~{fulfillment.delivery_eta_days} days")


def _print_summary(summary: CaseSummary) -> None:
    print(f"\n=== Case {summary.case_id}: {summary.outcome} ===")
    if summary.sourcing:
        _print_sourcing(summary.sourcing)
    if summary.order:
        _print_order(summary.order)
    if summary.fulfillment:
        _print_fulfillment(summary.fulfillment)
    for contact in summary.patient_contacts:
        state = "reached" if contact.reached else "NOT reached"
        questions = f"; questions: {'; '.join(contact.open_questions)}" if contact.open_questions else ""
        print(f"Patient ({contact.milestone}): {state}{questions}")
    if summary.escalations:
        print("Came back to a human for:")
        for escalation_id in summary.escalations:
            print(f"  {escalation_id}")
    if summary.closing_note:
        print(f"Closing note: {summary.closing_note}")
    callbacks = [c.escalation_id for c in summary.patient_contacts if c.escalation_id]
    if callbacks:
        print(f"{len(callbacks)} callback(s) still open for a person: uv run python -m mira.human list")


def _print_waiting_banner(review: PendingReview, namespace: str) -> None:
    print(f"\n{BANNER}\n⏸  CASE IS WAITING FOR A HUMAN\n{BANNER}")
    print(f"  Reason:  {review.reason}")
    for key, value in review.context.items():
        if key != "blocking":
            print(f"  {key}: {value}")
    print(f"  Temporal: HumanReviewWorkflow '{review.review_workflow_id}' is Running")
    print(f"            {_ui_link(namespace, review.review_workflow_id)}")
    print(f"  Elsewhere: uv run python -m mira.human decide '{review.escalation_id}' retry|close --note \"...\"")
    print(BANNER)


async def _ask(prompt: str) -> str:
    try:
        return (await asyncio.to_thread(input, prompt)).strip()
    except EOFError:
        return ""


async def _prompt_decision(client: Client, review: PendingReview) -> None:
    choice = (await _ask("  Decide now? [r]etry / [c]lose / [s]kip (decide later from another terminal): ")).lower()
    action = PROMPT_CHOICES.get(choice)
    if action is None:
        print("  Left open: the case keeps waiting until someone decides.")
        return
    note = await _ask("  Note for the case record (optional): ")
    try:
        review_id = await send_decision(client, review.escalation_id, action, note)
        print(f"  Sent {action} to {review_id}; the case resumes.\n")
    except ReviewNotFound as exc:
        print(f"  {exc}")


async def _poll_status(handle: WorkflowHandle) -> CaseStatus | None:
    try:
        return await handle.query(CaseWorkflow.status)
    except RPCError:
        return None


async def _follow(client: Client, handle: WorkflowHandle, interactive: bool) -> CaseSummary:
    result = asyncio.ensure_future(handle.result())
    last_stage = None
    announced: set[str] = set()
    while True:
        done, _ = await asyncio.wait({result}, timeout=STATUS_POLL_SECONDS)
        if done:
            return result.result()
        status = await _poll_status(handle)
        if status is None:
            continue
        if status.stage != last_stage:
            print(f"[{datetime.now():%H:%M:%S}] {status.stage}")
            last_stage = status.stage
        for review in status.pending_reviews:
            if review.escalation_id in announced:
                continue
            announced.add(review.escalation_id)
            _print_waiting_banner(review, client.namespace)
            if interactive:
                await _prompt_decision(client, review)


async def _run(interactive: bool) -> None:
    case = load_case()
    client = await connect()
    workflow_id = f"{case.case_id}-{datetime.now(UTC):%Y%m%d-%H%M%S}"
    handle = await client.start_workflow(
        CaseWorkflow.run,
        CaseInput(case=case, seconds_per_day=seconds_per_day()),
        id=workflow_id,
        task_queue=CASE_TASK_QUEUE,
        static_summary=f"DME case: {case.patient.name}, {case.equipment.description} ({case.equipment.hcpcs_code})",
    )
    print(f"Started {workflow_id}")
    print(_ui_link(client.namespace, workflow_id))
    _print_summary(await _follow(client, handle, interactive))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-prompt", action="store_true", help="never prompt; decide with `mira.human`")
    args = parser.parse_args()
    try:
        asyncio.run(_run(interactive=not args.no_prompt and sys.stdin.isatty()))
    except (ConfigError, DataError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
