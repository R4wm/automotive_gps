# Mileage tracking

`enviro-gps-speed` adds **distance from GPS position** only while the receiver looks like it is **actually driving** (not parked with speed jitter):

| Field | Meaning |
|--------|---------|
| **odometer_miles** | Total miles (never auto-reset) |
| **trip_miles** | Miles since last trip reset |

## Storage

1. **Redis** hash `automotive_gps:mileage` (inspect from any machine with Redis access)
2. **JSON file** `/var/lib/automotive_gps/odometer.json` (upload / backup if Redis is down)

On startup, the service merges file and Redis and keeps the **higher odometer** so you do not lose distance.

## Boot / display timing

On a Pi Zero, DietPi often reaches **`enviro-gps-speed`** around **~35–40 s** after power (networking / `ifupdown-pre` dominate). The LCD should show **`--`** within a few seconds of the service start; a **GPS speed number** can take longer (cold GPS fix). The service is configured to start after local storage is ready, not after WiFi.

## Pi setup

```bash
apt install redis-server python3-redis
mkdir -p /var/log/redis && chown redis:redis /var/log/redis
systemctl enable --now redis-server
```

If Redis fails with “Can't open the log file”, create `/var/log/redis` as above.

Configure `/etc/default/enviro-gps-speed` (see `config/enviro-gps-speed.default`). Remote Redis:

```bash
REDIS_URL=redis://user:pass@home.example.com:6379/0
```

Restart: `systemctl restart enviro-gps-speed`

## Check miles

```bash
automotive-gps-miles
automotive-gps-miles --export          # JSON for upload
automotive-gps-miles --reset-trip      # trip only
```

Redis CLI:

```bash
redis-cli HGETALL automotive_gps:mileage
```

## Upload later

Copy `/var/lib/automotive_gps/odometer.json` or pipe `automotive-gps-miles --export` into your sync script.

**Parked / indoor drift:** raw NMEA speed often reads 2–5 mph while you are still; the old speed integrator could add a fake mile overnight. Mileage now waits for **≥ ~5 mph for ~4 s** before counting, uses **lat/lon steps** between fixes, and stops after **≤ ~2 mph for ~45 s**. Tune via `/etc/default/enviro-gps-speed`:

```bash
MILEAGE_DRIVING_ENTER_MPH=5
MILEAGE_DRIVING_ENTER_SEC=4
MILEAGE_PARKED_MAX_MPH=2
MILEAGE_PARKED_HOLD_SEC=45
```

**Fix a bad odometer:** edit `/var/lib/automotive_gps/odometer.json` (`odometer_miles` / `trip_miles`), then `systemctl restart enviro-gps-speed`, or run `redis-cli DEL automotive_gps:mileage` if Redis still has the inflated value.

## Personal records

Tracked when GPS fix is **A** (valid):

| Record | Rule |
|--------|------|
| **1/8 mi** | Lowest time from **≥ 1 mph** until GPS path length reaches **0.125 mi** (drag-strip eighth) |
| **0–60** | Lowest time from **≥ 1 mph** to **≥ 60 mph** |
| **20–60** | Lowest time from **≥ 20 mph** to **≥ 60 mph** |
| **Top speed** | Maximum mph seen |

Stored in **`/var/lib/automotive_gps/records.json`** and Redis hash **`automotive_gps:records`**. At **0 mph**, the Enviro LCD **rotates two pages** every few seconds (default **4 s**, `--records-page-sec`): page 1 is **1/8**, **0–60**, **TOP**, **MI**; page 2 is **20–60** large. GPS distance is approximate vs a beam trip; best for comparing your own runs.
