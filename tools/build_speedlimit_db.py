#!/usr/bin/env python3
"""
Build a local speed-limit SQLite DB from an OpenStreetMap .osm.pbf extract.

Requires: pip install pyrosm shapely

Example (Virginia — adjust for your driving area):
  wget -O /tmp/virginia.osm.pbf https://download.geofabrik.de/north-america/us/virginia-latest.osm.pbf
  python3 tools/build_speedlimit_db.py --pbf /tmp/virginia.osm.pbf \\
      --output /var/lib/automotive_gps/speedlimits.db
"""

from __future__ import annotations

import argparse
import sqlite3
import struct
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from speedlimit_db import parse_maxspeed_mph  # noqa: E402

GEOFABRIK = "https://download.geofabrik.de/north-america/us/{region}-latest.osm.pbf"


def encode_geom(coords: list[tuple[float, float]]) -> bytes:
    """Pack lat/lon pairs: uint16 count + float32 lat, float32 lon ..."""
    capped = coords[:500]
    buf = struct.pack("<H", len(capped))
    for lat, lon in capped:
        buf += struct.pack("<ff", lat, lon)
    return buf


def download_region(region: str, dest: Path) -> None:
    url = GEOFABRIK.format(region=region)
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url} -> {dest}")
    urllib.request.urlretrieve(url, dest)  # noqa: S310


def build_db(pbf: Path, output: Path, *, region: str) -> None:
    try:
        from pyrosm import OSM
    except ImportError as exc:
        raise SystemExit(
            "Install build deps: pip install -r tools/requirements-build.txt"
        ) from exc

    print(f"Reading OSM driving network from {pbf} (this can take several minutes)...")
    osm = OSM(str(pbf))
    _nodes, edges = osm.get_network(
        network_type="driving",
        extra_attributes=["maxspeed", "name", "ref"],
    )
    if edges is None or edges.empty:
        raise SystemExit("No drivable edges returned from PBF.")

    if edges.crs is not None and str(edges.crs) != "EPSG:4326":
        edges = edges.to_crs("EPSG:4326")

    tagged = edges[edges["maxspeed"].notna()].copy()
    print(f"Edges with maxspeed tag: {len(tagged)} / {len(edges)}")

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()

    conn = sqlite3.connect(tmp)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE segments (
            id INTEGER PRIMARY KEY,
            maxspeed_mph REAL NOT NULL,
            min_lon REAL NOT NULL,
            min_lat REAL NOT NULL,
            max_lon REAL NOT NULL,
            max_lat REAL NOT NULL,
            geom BLOB NOT NULL
        );
        CREATE VIRTUAL TABLE segment_rtree USING rtree(
            id, min_lon, max_lon, min_lat, max_lat
        );
        """
    )

    inserted = 0
    skipped = 0
    t0 = time.time()
    for idx, row in tagged.iterrows():
        mph = parse_maxspeed_mph(str(row["maxspeed"]))
        if mph is None:
            skipped += 1
            continue
        geom = row.geometry
        if geom is None or geom.is_empty:
            skipped += 1
            continue
        if geom.geom_type == "LineString":
            lines = [geom]
        elif geom.geom_type == "MultiLineString":
            lines = list(geom.geoms)
        else:
            skipped += 1
            continue

        for line in lines:
            coords = [(float(y), float(x)) for x, y in line.coords]
            if len(coords) < 2:
                continue
            lats = [c[0] for c in coords]
            lons = [c[1] for c in coords]
            min_lat, max_lat = min(lats), max(lats)
            min_lon, max_lon = min(lons), max(lons)
            blob = encode_geom(coords)
            cur = conn.execute(
                """
                INSERT INTO segments (maxspeed_mph, min_lon, min_lat, max_lon, max_lat, geom)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (mph, min_lon, min_lat, max_lon, max_lat, blob),
            )
            seg_id = cur.lastrowid
            conn.execute(
                "INSERT INTO segment_rtree VALUES (?, ?, ?, ?, ?)",
                (seg_id, min_lon, max_lon, min_lat, max_lat),
            )
            inserted += 1

    conn.execute(
        "INSERT INTO meta VALUES (?, ?), (?, ?), (?, ?), (?, ?)",
        (
            "source",
            "OpenStreetMap contributors (ODbL)",
            "pbf",
            str(pbf),
            "region",
            region,
            "built_at",
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ),
    )
    conn.commit()
    conn.execute("ANALYZE")
    conn.close()
    tmp.replace(output)
    elapsed = time.time() - t0
    print(f"Done: {inserted} segments, {skipped} rows skipped, {elapsed:.1f}s")
    print(f"Database: {output} ({output.stat().st_size / 1_048_576:.1f} MiB)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build OSM speed-limit SQLite DB")
    parser.add_argument("--pbf", type=Path, help="Local .osm.pbf file")
    parser.add_argument(
        "--download-region",
        metavar="NAME",
        help="Geofabrik US state/territory slug (e.g. virginia, california)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/var/lib/automotive_gps/speedlimits.db"),
    )
    args = parser.parse_args()

    region = args.download_region or "custom"
    if args.download_region:
        pbf = Path(f"/tmp/osm-{args.download_region}-latest.osm.pbf")
        if not pbf.is_file():
            download_region(args.download_region, pbf)
    elif args.pbf:
        pbf = args.pbf
    else:
        parser.error("Provide --pbf or --download-region")

    build_db(pbf, args.output, region=region)


if __name__ == "__main__":
    main()
