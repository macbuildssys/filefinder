"""Turn a GPS position into place names, fully offline.

The data file lists about 60,000 towns and cities (population 5,000 and up) from
GeoNames (CC BY 4.0). We find the nearest one within 50 km. Photos taken far from
any town simply get no place.
"""
from __future__ import annotations

import csv
import gzip
import math
import os

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
MAX_DISTANCE_KM = 50.0
# Words that appear in many region names and say nothing useful.
FILLER = {"state", "of", "county", "region", "province", "district", "municipality",
          "governorate", "city", "the", "and"}

class PlaceFinder:
    def __init__(self, data_dir: str = DATA_DIR):
        self.places: list[tuple] = []
        self.grid: dict[tuple, list[int]] = {}
        self.countries: dict[str, str] = {}
        with open(os.path.join(data_dir, "countries.csv"), encoding="utf-8", newline="") as fh:
            for row in csv.reader(fh):
                if len(row) >= 2:
                    self.countries[row[0]] = row[1]
        with gzip.open(os.path.join(data_dir, "places.csv.gz"), "rt", encoding="utf-8", newline="") as fh:
            for lat, lon, city, state, code in csv.reader(fh):
                lat, lon = float(lat), float(lon)
                self.grid.setdefault((math.floor(lat), math.floor(lon)), []).append(len(self.places))
                self.places.append((lat, lon, city, state, code))

    def nearest(self, lat: float, lon: float):
        """The closest known town within MAX_DISTANCE_KM, or None."""
        row, col = math.floor(lat), math.floor(lon)
        # Near the poles a degree of longitude is short, so look further sideways.
        span = min(math.ceil(1 / max(math.cos(math.radians(lat)), 0.05)), 25)
        best, best_distance = None, MAX_DISTANCE_KM
        for dr in (-1, 0, 1):
            for dc in range(-span, span + 1):
                cell = (row + dr, ((col + dc + 180) % 360) - 180)
                for index in self.grid.get(cell, ()):
                    p = self.places[index]
                    d = _distance_km(lat, lon, p[0], p[1])
                    if d <= best_distance:
                        best, best_distance = p, d
        return best

    def words(self, lat: float, lon: float) -> list[str]:
        """Place name words for a position, for example: berlin, germany."""
        found = self.nearest(lat, lon)
        if not found:
            return []
        from .textutil import split_words
        city, state, code = found[2], found[3], found[4]
        words = split_words(f"{city} {state} {self.countries.get(code, '')}")
        unique = []
        for w in words:
            if w not in FILLER and w not in unique:
                unique.append(w)
        return unique

def _distance_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))

_finder: PlaceFinder | None = None

def place_words(lat: float, lon: float) -> list[str]:
    """Shared lookup. The data loads on first use (about a tenth of a second)."""
    global _finder
    if _finder is None:
        _finder = PlaceFinder()
    return _finder.words(lat, lon)
