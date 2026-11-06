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
The installer also adds a systemd `LogsDirectory=redis` drop-in so it is
recreated when Redis starts, including after DietPi clears volatile logs.

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

## Accuracy and filtering

Mileage uses checksum-validated, measured RMC fixes and **GPS UTC timestamps**.
Distance is the sum of accepted position steps, not raw speed integrated while
parked. Duplicate/backward timestamps, invalid fixes, gaps over **1.5 seconds**,
invalid numbers/coordinates, and impossible jumps break motion continuity.
Steps are capped at **0.12 miles** and **120 mph** implied speed by default, and
must also be consistent with average GPS speed (at most the greater of 3× that
speed or that speed + 15 mph). Lost-fix intervals are not estimated.

Driving requires **≥4 mph for 3 seconds** by default. Accepted distance during
that confirmation window is buffered and credited once driving is confirmed.
Speeds **≤2 mph** suppress distance immediately, including while the parked
hold timer runs. Sustained speeds below the driving-entry threshold end driving
after **45 seconds**; unchanged coordinates still advance that timer.

These conservative rules trade some very slow or interrupted movement for less
parked drift. GPS position noise and receiver filtering still affect accuracy;
curves between fixes can be undercounted. A known-distance road comparison with
a valid fix is required to measure the real-world error. Decimal places are
storage precision, not guaranteed GPS accuracy.

Optional tuning in `/etc/default/enviro-gps-speed`:

```bash
MILEAGE_DRIVING_ENTER_MPH=4
MILEAGE_DRIVING_ENTER_SEC=3
MILEAGE_PARKED_MAX_MPH=2
MILEAGE_PARKED_HOLD_SEC=45
```

## Persistence and maintenance

JSON writes use atomic replacement and sync the file and directory to disk.
Mileage still flushes after **30 seconds or 0.02 miles**, and on graceful stop;
abrupt power loss can lose the unflushed increment. Redis is retried on later
flushes if unavailable. CLI inspection is read-only and cannot overwrite newer
in-memory totals. Startup keeps the highest odometer and the newest trip,
including a trip that was reset to zero.

**TRIP resets daily:** the first GPS fix on a new local calendar day starts
TRIP from zero (the odometer keeps counting). The day comes from GPS time in
the Pi's time zone, so set it (`timedatectl set-timezone ...`); on UTC the trip
and the TODAY records roll over at UTC midnight.

To reset the trip by hand, stop the GPS service first so its live totals cannot overwrite
the reset, then restart it:

```bash
systemctl stop enviro-gps-speed
automotive-gps-miles --reset-trip
systemctl start enviro-gps-speed
```

## Speed records

Tracked from the same checksum-validated fixes, timed with **GPS UTC
timestamps** (so display work never changes a time):

| Record | Rule |
|--------|------|
| **1/8 mi** | From launch until **0.125 mi** covered (6–30 s) |
| **0–60** | From launch to crossing **60 mph** (4–30 s) |
| **20–60** | From crossing **20 mph** to crossing **60 mph** (2–30 s) |

- 0–60 and 1/8 mile need a **standing start**: a fix at or under 2 mph, then
  5 mph within 5 s of launch. They restart only after the next stop if speed
  falls back under 5 mph. A rolling start only counts for 20–60.
- **Launch** is estimated between fixes: the early acceleration is
  extrapolated back to 0 mph. At 1 fix per second the car can leave up to a
  second after the last stopped fix; timing from the 5 mph crossing (as before)
  read about 0.35 s quick.
- The 1/8 mile distance integrates GPS (Doppler) speed, which is far more
  precise than 1 Hz positions, and the receiver holds position still near
  standstill. The finish is solved within the last interval.
- 20–60 starts when speed is seen crossing 20 mph from below. Cruising near 60
  is never a run. Crossing times are interpolated between fixes, and a
  crossing of 60 needs a second fix at 60+.
- Lost fixes, gaps over 1.5 s, duplicate/backward timestamps, speed changes
  over 35 mph/s and gross position jumps cancel the run.
- No rollout is subtracted (drag strips and many 0–60 tests start after the
  first foot, which reads roughly 0.2–0.3 s quicker).
- The receiver reports once per second, so expect about ±0.1 s.

**Today** is the Pi's local calendar day of the GPS time. Records persist in
`/var/lib/automotive_gps/records.json` and Redis `automotive_gps:records`. At
0 mph the LCD rotates **MI/TRIP**, **TODAY**, **BEST EVER**, then the **clock
and IP address** on one page (default **4 s** each, `--stats-page-sec`). The
clock follows GPS time, since the Pi has no RTC. At the next stop (≤ 3 mph)
after a new daily or best-ever record, the LCD shows `jammin_shyguy.png`
(`--record-image`) with the time for **3 s** each (`--record-image-sec`, `0`
disables). It never covers the speedometer while moving.

Times stored with older timing (`timing_version` below 2, which started at
5 mph) are dropped on startup because they are not comparable. To clear
records by hand:

```bash
systemctl stop enviro-gps-speed
automotive-gps-miles --reset-records
systemctl start enviro-gps-speed
```


Run regression checks with:

```bash
python3 -m unittest discover -s tests -v
```
