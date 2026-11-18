import json
from pathlib import Path
from typing import Protocol

from pydantic import Field, ValidationError

from mira.models import CallDisposition, CallPurpose, CallRequest, Frozen, WrittenOrder


class RawCall(Frozen):
    disposition: CallDisposition
    transcript: str = ""


class VoiceAgent(Protocol):
    """Places one outbound call and returns what happened. A real implementation would wrap a telephony/voice-AI provider."""

    async def call(self, request: CallRequest) -> RawCall: ...


class ScriptedParty(Frozen):
    name: str
    calls: dict[CallPurpose, list[RawCall]]


class Scenario(Frozen):
    description: str
    parties: dict[str, ScriptedParty]
    written_orders: list[WrittenOrder] = Field(default_factory=list)


class ScenarioError(ValueError):
    pass


def load_scenario(path: Path) -> Scenario:
    try:
        return Scenario.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError as exc:
        raise ScenarioError(f"Scenario file not found: {path}") from exc
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ScenarioError(f"Invalid scenario file {path.name}: {exc}") from exc


class MockVoiceAgent:
    """Replays scripted calls keyed by phone, purpose and attempt.

    Keying on the attempt number (not an internal counter) keeps it deterministic
    under Temporal activity retries. Unscripted parties never pick up.
    """

    def __init__(self, scenario: Scenario) -> None:
        self._scenario = scenario

    @classmethod
    def from_file(cls, path: Path) -> "MockVoiceAgent":
        return cls(load_scenario(path))

    async def call(self, request: CallRequest) -> RawCall:
        party = self._scenario.parties.get(request.phone)
        turns = party.calls.get(request.purpose, []) if party else []
        if not turns:
            return RawCall(disposition=CallDisposition.NO_ANSWER)
        return turns[min(request.attempt, len(turns)) - 1]
