import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mira.config import EVENT_LOG_PATH


class EventLog:
    """Append-only JSONL case log; stands in for a database."""

    def __init__(self, path: Path = EVENT_LOG_PATH) -> None:
        self._path = path

    def append(self, case_id: str, kind: str, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now(UTC).isoformat(), "case_id": case_id, "kind": kind, **payload}
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
