import pytest

from mira.config import SCENARIOS_DIR
from mira.models import CallPurpose, CallRequest
from mira.voice.agent import Scenario, load_scenario

LINCOLN_PARK_PHONE = "(312) 555-0468"
UNSCRIPTED_PHONE = "(847) 555-0533"


@pytest.fixture
def scenario() -> Scenario:
    return load_scenario(SCENARIOS_DIR / "default.json")


def make_request(phone: str, purpose: CallPurpose, attempt: int = 1, party: str = "Test Party") -> CallRequest:
    return CallRequest(
        case_id="case-test",
        call_id=f"call-{purpose}-{attempt}",
        purpose=purpose,
        party_name=party,
        phone=phone,
        attempt=attempt,
    )
