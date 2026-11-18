import json
from pathlib import Path

import pytest
from tests.conftest import LINCOLN_PARK_PHONE, make_request
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from mira.activities import CallActivities
from mira.config import ConfigError
from mira.events import EventLog
from mira.models import CallDisposition, CallPurpose
from mira.voice.agent import MockVoiceAgent, Scenario


async def _missing_key(*_: object) -> None:
    raise ConfigError("OPENROUTER_API_KEY is not set")


async def test_place_call_logs_outcome(scenario: Scenario, tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"
    activities = CallActivities(MockVoiceAgent(scenario), EventLog(log_path))

    outcome = await ActivityEnvironment().run(
        activities.place_call, make_request(LINCOLN_PARK_PHONE, CallPurpose.SUPPLIER_SCREEN, attempt=1)
    )

    assert outcome.disposition is CallDisposition.NO_ANSWER
    event = json.loads(log_path.read_text().strip())
    assert event["kind"] == "call_completed"
    assert event["disposition"] == "no_answer"


async def test_missing_llm_config_is_non_retryable(scenario: Scenario, tmp_path: Path) -> None:
    activities = CallActivities(MockVoiceAgent(scenario), EventLog(tmp_path / "e.jsonl"), complete=_missing_key)
    request = make_request(LINCOLN_PARK_PHONE, CallPurpose.SUPPLIER_SCREEN, attempt=2)

    with pytest.raises(ApplicationError) as exc_info:
        await ActivityEnvironment().run(activities.place_call, request)

    assert exc_info.value.non_retryable
