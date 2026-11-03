"""Local SQLite speed-limit lookup (built from OpenStreetMap)."""

from __future__ import annotations

import math
import re
import sqlite3
import struct
from pathlib import Path

DEFAULT_DB_PATH = Path("/var/lib/automotive_gps/speedlimits.db")

_MPH_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*mph$", re.I)
_KPH_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*kph$", re.I)
_KMH_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*km/h$", re.I)
_NUM_RE = re.compile(r"^(\d+(?:\.\d+)?)$")
_US_RE = re.compile(r"^US:(\d+(?:\.\d+)?)$", re.I)


def parse_maxspeed_mph(raw: str | None, *, default_country_mph: bool = True) -> float | None:
    """Parse OSM maxspeed tag to mph. Returns None if unknown / variable."""
    if raw is None:
        return None
    tag = raw.strip()
    if not tag or tag.lower() in ("signals", "variable", "none", "walk", "yes"):
        return None
    m = _MPH_RE.match(tag)
    if m:
        return float(m.group(1))
    m = _KPH_RE.match(tag) or _KMH_RE.match(tag)
    if m:
        return float(m.group(1)) * 0.621371
    m = _US_RE.match(tag)
    if m:
        return float(m.group(1))
    m = _NUM_RE.match(tag)
    if m:
        value = float(m.group(1))
        # Geofabrik US extracts: bare numbers are usually mph.
        return value if default_country_mph else value * 0.621371
    return None


def _deg2rad(d: float) -> float:
    return d * math.pi / 180.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    dlat = _deg2rad(lat2 - lat1)
    dlon = _deg2rad(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(_deg2rad(lat1))
        * math.cos(_deg2rad(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _decode_geom(blob: bytes) -> list[tuple[float, float]]:
    if len(blob) < 2:
        return []
    (count,) = struct.unpack_from("<H", blob, 0)
    pts: list[tuple[float, float]] = []
    off = 2
    for _ in range(count):
        lat, lon = struct.unpack_from("<ff", blob, off)
        pts.append((lat, lon))
        off += 8
    return pts


def _point_segment_distance_m(
    plat: float, plon: float, a: tuple[float, float], b: tuple[float, float]
) -> float:
    ax, ay = a[0], a[1]
    bx, by = b[0], b[1]
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return haversine_m(plat, plon, ax, ay)
    t = ((plat - ax) * dx + (plon - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    clat = ax + t * dx
    clon = ay + t * dy
    return haversine_m(plat, plon, clat, clon)


class SpeedLimitDB:
    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        return self._conn

    def meta(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        conn = self._connection()
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
        return {str(k): str(v) for k, v in rows}

    def lookup_mph(
        self,
        lat: float,
        lon: float,
        *,
        search_m: float = 45.0,
        max_candidates: int = 32,
    ) -> tuple[float | None, float | None]:
        """
        Return (maxspeed_mph, distance_m) for nearest tagged segment within search_m.
        """
        if not self.path.is_file():
            return None, None

        conn = self._connection()
        # ~1 deg lat ≈ 111 km; pad bbox by search radius.
        pad = search_m / 111_000.0
        min_lon, max_lon = lon - pad, lon + pad
        min_lat, max_lat = lat - pad, lat + pad

        rows = conn.execute(
            """
            SELECT s.id, s.maxspeed_mph, s.geom
            FROM segment_rtree r
            JOIN segments s ON s.id = r.id
            WHERE r.max_lon >= ? AND r.min_lon <= ?
              AND r.max_lat >= ? AND r.min_lat <= ?
            LIMIT ?
            """,
            (min_lon, max_lon, min_lat, max_lat, max_candidates),
        ).fetchall()

        best_mph: float | None = None
        best_dist = search_m + 1.0
        for _sid, mph, geom_blob in rows:
            pts = _decode_geom(geom_blob)
            if len(pts) < 2:
                continue
            for i in range(len(pts) - 1):
                dist = _point_segment_distance_m(lat, lon, pts[i], pts[i + 1])
                if dist < best_dist:
                    best_dist = dist
                    best_mph = float(mph)

        if best_mph is None or best_dist > search_m:
            return None, None
        return best_mph, best_dist
