# Local OSM speed limits

Use **OpenStreetMap** `maxspeed` tags offline: download a regional extract, build a **SQLite** database once, copy it to the Pi, and query by GPS lat/lon (no live API in the car).

**License:** OSM data is **[ODbL](https://www.openstreetmap.org/copyright)** — keep attribution if you ship the DB (e.g. “© OpenStreetMap contributors”).

## 1. Pick a region (keep it small)

Download only where you drive. [Geofabrik](https://download.geofabrik.de/) US states work well:

| Example | PBF size (approx.) |
|---------|---------------------|
| Single US state | ~20–200 MB |
| Full US | ~10+ GB (avoid on Pi Zero) |

List: https://download.geofabrik.de/north-america/us/

## 2. Build the database (laptop / NUC, not necessarily the Pi)

```bash
cd automotive_gps
python3 -m venv .venv-build
source .venv-build/bin/activate
pip install -r tools/requirements-build.txt

# Example: Virginia
python3 tools/build_speedlimit_db.py --download-region virginia \
  --output ./speedlimits-virginia.db
```

Or use your own PBF:

```bash
wget -O /tmp/my.osm.pbf 'https://download.geofabrik.de/...'
python3 tools/build_speedlimit_db.py --pbf /tmp/my.osm.pbf \
  --output /var/lib/automotive_gps/speedlimits.db
```

Build time: a few minutes per state on a modern PC.

## 3. Install on the Pi

```bash
scp speedlimits-virginia.db root@10.0.0.161:/var/lib/automotive_gps/speedlimits.db
chmod 644 /var/lib/automotive_gps/speedlimits.db
```

Runtime on the Pi needs **only** the `.db` file (stdlib `sqlite3`).

## 4. Test lookup

```bash
speedlimit-lookup 37.5407 -77.4360   # lat lon
```

Returns posted mph when OSM has `maxspeed` on the nearest road (~45 m search).

## 5. Keep it updated

OSM changes daily; rebuild **weekly or monthly** on a PC:

```bash
python3 tools/build_speedlimit_db.py --download-region virginia \
  --output speedlimits-virginia.db
scp speedlimits-virginia.db root@PI:/var/lib/automotive_gps/speedlimits.db
```

Optional cron on a home server (not the Pi):

```cron
0 4 1 * * cd /path/automotive_gps && .venv-build/bin/python3 tools/build_speedlimit_db.py --download-region virginia --output /var/lib/automotive_gps/speedlimits.db
```

## Limits / expectations

- **Coverage gaps:** many US roads have no `maxspeed` in OSM → lookup returns nothing.
- **Wrong tags:** community-mapped; verify critical routes.
- **Not real-time:** construction / temporary limits won’t appear until mappers update OSM.
- **Multi-state trips:** build a merged region, or ship multiple DBs and pick by GPS bbox (future work).

## Wiring into the car display

Not enabled in `enviro-gps-speed` yet. Next steps:

1. Parse **lat/lon** from `$GPRMC` when fix is `A`.
2. Call `SpeedLimitDB.lookup_mph(lat, lon)` every ~5–10 s while moving.
3. Show limit next to speed (and optional over-limit hint).

See `lib/speedlimit_db.py` and `tools/build_speedlimit_db.py`.
