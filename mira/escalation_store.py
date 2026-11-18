"""JSON-file store for escalations (stands in for a table). Writes are atomic: temp file + rename."""

import json
import os
from pathlib import Path
from typing import Any

from mira.config import ESCALATIONS_PATH
from mira.models import Escalation

OPEN = "open"


class EscalationStoreError(RuntimeError):
    pass


class EscalationStore:
    def __init__(self, path: Path = ESCALATIONS_PATH) -> None:
        self._path = path

    def all(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise EscalationStoreError(f"Escalations file {self._path} is corrupt: {exc}") from exc

    def get(self, escalation_id: str) -> dict[str, Any] | None:
        return next((e for e in self.all() if e.get("escalation_id") == escalation_id), None)

    def get_escalation(self, escalation_id: str) -> Escalation | None:
        record = self.get(escalation_id)
        return Escalation.model_validate({k: record[k] for k in Escalation.model_fields}) if record else None

    def add(self, escalation: Escalation) -> bool:
        """Returns False if this id already exists, so retried activities never duplicate a task."""
        existing = self.all()
        if any(e.get("escalation_id") == escalation.escalation_id for e in existing):
            return False
        self._write([*existing, {**escalation.model_dump(mode="json"), "status": OPEN}])
        return True

    def update(self, escalation_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        records = self.all()
        updated = [{**e, **fields} if e.get("escalation_id") == escalation_id else e for e in records]
        self._write(updated)
        return next((e for e in updated if e.get("escalation_id") == escalation_id), None)

    def open_callbacks(self) -> list[Escalation]:
        return [
            Escalation.model_validate({k: e[k] for k in Escalation.model_fields})
            for e in self.all()
            if e.get("status") == OPEN and not is_blocking(e)
        ]

    def _write(self, records: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(records, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)


def is_blocking(record: dict[str, Any]) -> bool:
    return record.get("context", {}).get("blocking") == "true"
