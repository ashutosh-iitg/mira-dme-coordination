import json
import math
from pathlib import Path

from mira.config import DATA_DIR
from mira.data import DataError
from mira.models import RankedSupplier, Supplier

ZIP_CENTROIDS_PATH = DATA_DIR / "zip_centroids.json"
EARTH_RADIUS_MILES = 3958.8

Coordinate = tuple[float, float]


def load_zip_centroids(path: Path = ZIP_CENTROIDS_PATH) -> dict[str, Coordinate]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))["centroids"]
        return {zip_code: (float(lat), float(lon)) for zip_code, (lat, lon) in raw.items()}
    except (FileNotFoundError, KeyError, ValueError, TypeError) as exc:
        raise DataError(f"Invalid ZIP centroid file {path}: {exc}") from exc


def haversine_miles(a: Coordinate, b: Coordinate) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(h))


def rank_by_distance(
    suppliers: tuple[Supplier, ...],
    patient_zip: str,
    centroids: dict[str, Coordinate],
    excluded_phones: frozenset[str] = frozenset(),
) -> list[RankedSupplier]:
    """Closest first; suppliers with unknown ZIPs go last. Ties keep directory order."""
    if patient_zip not in centroids:
        raise DataError(f"No centroid for patient ZIP {patient_zip}")
    origin = centroids[patient_zip]

    ranked = [
        RankedSupplier(
            supplier=s,
            distance_miles=round(haversine_miles(origin, centroids[s.zip_code]), 1) if s.zip_code in centroids else None,
        )
        for s in suppliers
        if s.phone not in excluded_phones
    ]
    return sorted(ranked, key=lambda r: (r.distance_miles is None, r.distance_miles or 0.0))
