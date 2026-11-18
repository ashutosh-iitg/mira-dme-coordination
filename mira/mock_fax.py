from mira.models import WrittenOrder
from mira.voice.agent import Scenario


class MockFaxInbox:
    """Stand-in for the fax line where signed orders arrive. Returns the scenario's nth order, or None if nothing came."""

    def __init__(self, scenario: Scenario) -> None:
        self._orders = scenario.written_orders

    def fetch(self, fetch_attempt: int) -> WrittenOrder | None:
        if fetch_attempt < 1 or fetch_attempt > len(self._orders):
            return None
        return self._orders[fetch_attempt - 1]
