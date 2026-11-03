# Mileage tracking

`enviro-gps-speed` integrates GPS speed (mph) over time while you drive and stores:

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

Only speed **≥ 1 mph** with a GPS fix counts (idle / parking jitter is ignored).
