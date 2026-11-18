import asyncio
import logging

from temporalio.worker import Worker

from mira.activities import CallActivities
from mira.case_activities import CaseActivities
from mira.config import CASE_TASK_QUEUE, PHONE_LINES, VOICE_CALL_TASK_QUEUE, scenario_path
from mira.events import EventLog
from mira.mock_fax import MockFaxInbox
from mira.temporal_client import connect
from mira.voice.agent import MockVoiceAgent, load_scenario
from mira.workflows.case import CaseWorkflow
from mira.workflows.fulfillment import FulfillmentWorkflow
from mira.workflows.human_review import HumanReviewWorkflow
from mira.workflows.pcp_order import PcpOrderWorkflow
from mira.workflows.sourcing import SupplierSourcingWorkflow

logger = logging.getLogger(__name__)

WORKFLOWS = [CaseWorkflow, SupplierSourcingWorkflow, PcpOrderWorkflow, FulfillmentWorkflow, HumanReviewWorkflow]


def build_activities(events: EventLog) -> tuple[CallActivities, CaseActivities]:
    scenario = load_scenario(scenario_path())
    return CallActivities(MockVoiceAgent(scenario), events), CaseActivities(events, MockFaxInbox(scenario))


def case_activity_methods(case: CaseActivities) -> list:
    return [
        case.rank_suppliers,
        case.send_order_request,
        case.fetch_written_order,
        case.send_order_to_supplier,
        case.escalate,
        case.get_escalation,
        case.resolve_escalation,
        case.record_event,
    ]


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    client = await connect()
    calls, case = build_activities(EventLog())

    case_worker = Worker(client, task_queue=CASE_TASK_QUEUE, workflows=WORKFLOWS, activities=case_activity_methods(case))
    voice_worker = Worker(
        client,
        task_queue=VOICE_CALL_TASK_QUEUE,
        activities=[calls.place_call],
        max_concurrent_activities=PHONE_LINES,
    )
    logger.info("Workers up on %s and %s (%d phone lines)", CASE_TASK_QUEUE, VOICE_CALL_TASK_QUEUE, PHONE_LINES)
    await asyncio.gather(case_worker.run(), voice_worker.run())


if __name__ == "__main__":
    asyncio.run(main())
