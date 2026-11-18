import pytest

from mira.data import DataError, load_suppliers
from mira.ranking import load_zip_centroids, rank_by_distance


def test_ranks_same_zip_first_and_suburbs_last() -> None:
    ranked = rank_by_distance(load_suppliers(), "60614", load_zip_centroids())
    names = [r.supplier.supplier_name for r in ranked]

    assert names[:3] == ["Lakeshore Home Medical Equipment", "Lincoln Park DME Services", "Halsted Home Health Supply"]
    assert set(names[-2:]) == {"Evanston Home Medical", "Midwest Mobility Solutions"}
    assert ranked[0].distance_miles == 0.0


def test_excluded_suppliers_are_skipped() -> None:
    ranked = rank_by_distance(load_suppliers(), "60614", load_zip_centroids(), frozenset({"(312) 555-0142"}))

    assert all(r.supplier.phone != "(312) 555-0142" for r in ranked)
    assert len(ranked) == 11


def test_unknown_patient_zip_fails_fast() -> None:
    with pytest.raises(DataError, match="99999"):
        rank_by_distance(load_suppliers(), "99999", load_zip_centroids())
