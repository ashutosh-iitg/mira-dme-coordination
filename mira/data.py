import csv
import json
import re
from pathlib import Path

from pydantic import ValidationError

from mira.config import CASE_PATH, SUPPLIER_DIRECTORY_PATH
from mira.models import Case, Supplier

ZIP_PATTERN = re.compile(r"\b(\d{5})\s*$")
REQUIRED_SUPPLIER_COLUMNS = {"supplier_name", "phone", "address"}


class DataError(ValueError):
    pass


def load_suppliers(path: Path = SUPPLIER_DIRECTORY_PATH) -> tuple[Supplier, ...]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            missing = REQUIRED_SUPPLIER_COLUMNS - set(reader.fieldnames or [])
            if missing:
                raise DataError(f"{path.name} is missing columns: {sorted(missing)}")
            return tuple(_parse_supplier(row, line) for line, row in enumerate(reader, start=2))
    except FileNotFoundError as exc:
        raise DataError(f"Supplier directory not found at {path}") from exc


def _parse_supplier(row: dict[str, str], line: int) -> Supplier:
    address = (row.get("address") or "").strip()
    zip_match = ZIP_PATTERN.search(address)
    if not zip_match:
        raise DataError(f"Line {line}: no ZIP code in address {address!r}")
    try:
        return Supplier(
            supplier_name=row["supplier_name"].strip(),
            phone=row["phone"].strip(),
            address=address,
            zip_code=zip_match.group(1),
        )
    except ValidationError as exc:
        raise DataError(f"Line {line}: invalid supplier row: {exc}") from exc


def load_case(path: Path = CASE_PATH) -> Case:
    try:
        return Case.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError as exc:
        raise DataError(f"Case file not found at {path}") from exc
    except (json.JSONDecodeError, ValidationError) as exc:
        raise DataError(f"Invalid case file {path.name}: {exc}") from exc
