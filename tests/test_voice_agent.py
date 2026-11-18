from tests.conftest import LINCOLN_PARK_PHONE, UNSCRIPTED_PHONE, make_request

from mira.models import CallDisposition, CallPurpose
from mira.voice.agent import MockVoiceAgent, Scenario


async def test_replays_turn_matching_attempt_number(scenario: Scenario) -> None:
    agent = MockVoiceAgent(scenario)

    first = await agent.call(make_request(LINCOLN_PARK_PHONE, CallPurpose.SUPPLIER_SCREEN, attempt=1))
    second = await agent.call(make_request(LINCOLN_PARK_PHONE, CallPurpose.SUPPLIER_SCREEN, attempt=2))

    assert first.disposition is CallDisposition.NO_ANSWER
    assert second.disposition is CallDisposition.CONNECTED
    assert "put us down" in second.transcript


async def test_attempts_past_script_repeat_last_turn(scenario: Scenario) -> None:
    agent = MockVoiceAgent(scenario)

    result = await agent.call(make_request(LINCOLN_PARK_PHONE, CallPurpose.SUPPLIER_SCREEN, attempt=9))

    assert result.disposition is CallDisposition.CONNECTED


async def test_unscripted_party_never_answers(scenario: Scenario) -> None:
    agent = MockVoiceAgent(scenario)

    result = await agent.call(make_request(UNSCRIPTED_PHONE, CallPurpose.SUPPLIER_SCREEN))

    assert result.disposition is CallDisposition.NO_ANSWER
    assert result.transcript == ""
