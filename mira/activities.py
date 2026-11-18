from temporalio import activity
from temporalio.exceptions import ApplicationError

from mira.config import ConfigError
from mira.events import EventLog
from mira.extraction import StructuredCompleter, interpret_call
from mira.llm import complete_structured
from mira.models import CallOutcome, CallRequest
from mira.voice.agent import VoiceAgent


class CallActivities:
    def __init__(self, agent: VoiceAgent, events: EventLog, complete: StructuredCompleter = complete_structured) -> None:
        self._agent = agent
        self._events = events
        self._complete = complete

    @activity.defn
    async def place_call(self, request: CallRequest) -> CallOutcome:
        try:
            raw = await self._agent.call(request)
            outcome = await interpret_call(request, raw, self._complete)
        except ConfigError as exc:
            raise ApplicationError(str(exc), type="ConfigError", non_retryable=True) from exc

        result = outcome.supplier or outcome.followup or outcome.pcp or outcome.patient
        self._events.append(
            request.case_id,
            "call_completed",
            {
                "call_id": request.call_id,
                "purpose": request.purpose,
                "party": request.party_name,
                "attempt": request.attempt,
                "disposition": outcome.disposition,
                "confidence": outcome.confidence,
                "result": result.model_dump(mode="json") if result else None,
            },
        )
        return outcome
