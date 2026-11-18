from pathlib import Path

import pytest

from mira.escalation_store import EscalationStore
from mira.models import Escalation
from mira.reviews import ReviewNotFound, complete_callback


def _escalation(escalation_id: str, blocking: bool) -> Escalation:
    return Escalation(
        escalation_id=escalation_id, case_id="case-1", reason="r", context={"blocking": str(blocking).lower()}
    )


def test_add_is_idempotent_and_writes_open_status(tmp_path: Path) -> None:
    store = EscalationStore(tmp_path / "escalations.json")

    assert store.add(_escalation("e1", blocking=False)) is True
    assert store.add(_escalation("e1", blocking=False)) is False
    assert [r["status"] for r in store.all()] == ["open"]
    assert not (tmp_path / "escalations.tmp").exists()


def test_only_open_non_blocking_items_are_callbacks(tmp_path: Path) -> None:
    store = EscalationStore(tmp_path / "escalations.json")
    store.add(_escalation("callback", blocking=False))
    store.add(_escalation("review", blocking=True))

    assert [e.escalation_id for e in store.open_callbacks()] == ["callback"]


def test_blocking_review_cannot_be_completed_as_a_callback(tmp_path: Path) -> None:
    store = EscalationStore(tmp_path / "escalations.json")
    store.add(_escalation("review", blocking=True))

    with pytest.raises(ReviewNotFound, match="use `decide`"):
        complete_callback(store, "review", "")
    assert store.get("review")["status"] == "open"
