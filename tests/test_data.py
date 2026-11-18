from pathlib import Path

import pytest

from mira.data import DataError, load_case, load_suppliers


def test_loads_all_suppliers_with_zip_from_address() -> None:
    suppliers = load_suppliers()

    assert len(suppliers) == 12
    assert suppliers[0].zip_code == "60614"
    assert {s.zip_code for s in suppliers} >= {"60201", "60714"}


def test_rejects_directory_missing_required_column(tmp_path: Path) -> None:
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("supplier_name,phone\nAcme,(312) 555-0000\n")

    with pytest.raises(DataError, match="missing columns"):
        load_suppliers(csv_path)


def test_rejects_address_without_zip(tmp_path: Path) -> None:
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text('supplier_name,phone,address\nAcme,(312) 555-0000,"1 Main St, Chicago, IL"\n')

    with pytest.raises(DataError, match="no ZIP"):
        load_suppliers(csv_path)


def test_loads_eleanor_case() -> None:
    case = load_case()

    assert case.patient.name == "Eleanor Martinez"
    assert case.equipment.hcpcs_code == "K0001"
    assert case.patient.has_supplemental is False
