import asyncio
import logging

from temporalio.worker import Worker

from mira.coordinator.activities import CoordinatorActivities
from mira.coordinator.workflow import TASK_QUEUE, CaseCoordinatorWorkflow
from mira.temporal_client import connect


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    activities = CoordinatorActivities()
    async with Worker(
        await connect(), task_queue=TASK_QUEUE, workflows=[CaseCoordinatorWorkflow],
        activities=[activities.sync, activities.plan, activities.apply, activities.failure],
    ):
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
