"""Personal speed records (1/8 mi, 0-60, 20-60): today's and best ever."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from mileage import (  # same install dir on Pi
    DEFAULT_KEY_PREFIX,
    DEFAULT_REDIS_URL,
    MAX_SAMPLE_GAP_SEC,
    _utc_now,
    default_device_id,
    haversine_miles,
    local_day,
    redis,
)

DEFAULT_STATE_PATH = Path("/var/lib/automotive_gps/records.json")
REDIS_HASH_KEY = "records"

RECORD_KINDS = ("eighth_mile", "zero_sixty", "twenty_sixty")
RECORD_LABELS = {"eighth_mile": "1/8", "zero_sixty": "0-60", "twenty_sixty": "20-60"}

# Bump when timing changes; stored times from another version are dropped.
TIMING_VERSION = 2

STOPPED_MPH = 2.0
RUN_START_MPH = 5.0
RUN_20_MPH = 20.0
RUN_END_MPH = 60.0
EIGHTH_MILE_MI = 0.125
# A standing start must show 5 mph within this long of the estimated launch.
LAUNCH_WINDOW_SEC = 5.0
# Plausible (min, max) seconds; shorter is a glitch, longer is not a run.
RUN_LIMITS_SEC = {
    "eighth_mile": (6.0, 30.0),
    "zero_sixty": (4.0, 30.0),
    "twenty_sixty": (2.0, 30.0),
}
MAX_CREDIBLE_MPH = 165.0
# ~1.6 g; faster speed changes between fixes are receiver glitches.
MAX_ACCEL_MPH_PER_SEC = 35.0


def _crossing_time(t0: float, v0: float, t1: float, v1: float, target: float) -> float:
    """Interpolate when speed passed target between two fixes."""
    if v1 == v0:
        return t1
    return t0 + (target - v0) / (v1 - v0) * (t1 - t0)


def _segment_miles(v0: float, v1: float, seconds: float) -> float:
    return (v0 + v1) * 0.5 * seconds / 3600.0


def _time_to_cover(t0: float, v0: float, t1: float, v1: float, miles: float) -> float:
    """When the car covered miles after t0, with speed changing linearly to t1."""
    target = miles * 3600.0  # mph-seconds
    accel = (v1 - v0) / (t1 - t0)
    if abs(accel) < 1e-9:
        return t0 + target / v0
    return t0 + (math.sqrt(max(v0 * v0 + 2.0 * accel * target, 0.0)) - v0) / accel


def _launch_time(fixes: list[tuple[float, float]]) -> float:
    """Estimate when the car left a stop.

    fixes holds the last stopped fix and the first two moving ones. The
    second moving interval gives the launch acceleration (the first one may
    still contain standing time); extrapolating back to 0 mph finds the
    launch, kept between where the first interval's line reaches 0 mph and
    the first moving fix.
    """
    (ts, vs), (t1, v1), (t2, v2) = fixes
    earliest = ts - vs * (t1 - ts) / (v1 - vs)
    accel = (v2 - v1) / (t2 - t1)
    launch = t1 - v1 / accel if accel > 0 else earliest
    return min(max(launch, earliest), t1)


class RunTracker:
    """Time 1/8 mile, 0-60 and 20-60 runs from consecutive GPS fixes.

    Times use receiver UTC. 0-60 and 1/8 mile need a standing start: a fix
    at or below 2 mph, then 5 mph within a few seconds. They are timed from
    the launch estimated by extrapolating the early acceleration back to
    0 mph, because at 1 fix per second the car can leave the line up to a
    second after the last stopped fix. The 1/8 mile distance integrates
    Doppler speed rather than summing positions, which are noisy and held
    still by the receiver near standstill. 20-60 is a rolling run from an
    observed 20 mph crossing. Threshold crossings interpolate between fixes.
    """

    def __init__(self) -> None:
        self.invalidate()

    def invalidate(self) -> None:
        self._last: tuple[float, float, float, float] | None = None
        self._starts: dict[str, float] = {}
        self._finish_at: dict[str, float] = {}
        self._launch_fixes: list[tuple[float, float]] | None = None
        self._launch_at: float | None = None
        self._launched = False
        self._launch_mi = 0.0

    def _abort(self, kind: str) -> None:
        self._starts.pop(kind, None)
        self._finish_at.pop(kind, None)

    def _complete(self, kind: str, finish: float, out: list[tuple[str, float]]) -> None:
        elapsed = finish - self._starts[kind]
        low, high = RUN_LIMITS_SEC[kind]
        if low <= elapsed <= high:
            out.append((kind, elapsed))
        self._abort(kind)

    def _stopped(self, now: float, mph: float) -> None:
        self._launch_fixes = [(now, mph)]
        self._launch_at = None
        self._launched = False
        self._launch_mi = 0.0
        self._abort("zero_sixty")
        self._abort("eighth_mile")

    def _track_launch(
        self, t0: float, v0: float, now: float, mph: float, out: list[tuple[str, float]]
    ) -> None:
        fixes = self._launch_fixes
        if fixes is None:
            return
        if self._launch_at is None:
            fixes.append((now, mph))
            if len(fixes) < 3:
                return
            self._launch_at = _launch_time(fixes)
            (t1, v1), (t2, v2) = fixes[1], fixes[2]
            self._launch_mi = _segment_miles(0.0, v1, t1 - self._launch_at)
            self._launch_mi += _segment_miles(v1, v2, t2 - t1)
        else:
            before = self._launch_mi
            self._launch_mi += _segment_miles(v0, mph, now - t0)
            if "eighth_mile" in self._starts and self._launch_mi >= EIGHTH_MILE_MI:
                finish = _time_to_cover(t0, v0, now, mph, EIGHTH_MILE_MI - before)
                self._complete("eighth_mile", finish, out)

        if self._launched:
            if mph < RUN_START_MPH:
                self._abort("zero_sixty")
                self._abort("eighth_mile")
                self._launch_fixes = None
        elif mph >= RUN_START_MPH:
            self._launched = True
            if now - self._launch_at <= LAUNCH_WINDOW_SEC:
                self._starts["zero_sixty"] = self._starts["eighth_mile"] = self._launch_at
            else:
                self._launch_fixes = None  # rolled away too slowly

    def tick(self, mph: float, now: float, lat: float, lon: float) -> list[tuple[str, float]]:
        if (
            not all(math.isfinite(v) for v in (mph, now, lat, lon))
            or not 0 <= mph <= MAX_CREDIBLE_MPH
            or not -90 <= lat <= 90 or not -180 <= lon <= 180
        ):
            self.invalidate()
            return []
        last = self._last
        if last is not None:
            dt = now - last[0]
            if dt <= 0:
                self.invalidate()
                return []
            delta = haversine_miles(last[2], last[3], lat, lon)
            average_speed = (last[1] + mph) * 0.5
            if (
                dt > MAX_SAMPLE_GAP_SEC
                or abs(mph - last[1]) / dt > MAX_ACCEL_MPH_PER_SEC
                # Loose: the receiver holds position still at low speed and
                # then catches up, so only gross jumps count as glitches.
                or delta * 3600.0 / dt > max(average_speed * 3, average_speed + 30)
            ):
                # Never time across a gap or glitch; start fresh from here.
                self.invalidate()
                last = None
        self._last = (now, mph, lat, lon)
        if mph <= STOPPED_MPH:
            self._stopped(now, mph)
        if last is None:
            return []

        t0, v0 = last[0], last[1]
        completed: list[tuple[str, float]] = []

        if mph > STOPPED_MPH:
            self._track_launch(t0, v0, now, mph, completed)

        if v0 < RUN_20_MPH <= mph:
            self._starts["twenty_sixty"] = _crossing_time(t0, v0, now, mph, RUN_20_MPH)
            self._finish_at.pop("twenty_sixty", None)
        elif mph < RUN_20_MPH:
            self._abort("twenty_sixty")

        for kind in ("zero_sixty", "twenty_sixty"):
            if kind not in self._starts:
                continue
            if kind in self._finish_at:
                # A second fix at 60+ confirms the crossing (rejects one-fix spikes).
                if mph >= RUN_END_MPH:
                    self._complete(kind, self._finish_at[kind], completed)
                else:
                    del self._finish_at[kind]
            elif v0 < RUN_END_MPH <= mph:
                self._finish_at[kind] = _crossing_time(t0, v0, now, mph, RUN_END_MPH)

        for kind, start in list(self._starts.items()):
            if now - start > RUN_LIMITS_SEC[kind][1]:
                self._abort(kind)
        return completed


@dataclass
class NewRecord:
    kind: str
    seconds: float
    scope: str  # "best" (also today's) or "today"


class PersonalRecordsStore:
    """Best-ever and today's records; persisted to JSON and optional Redis."""

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        state_path: Path | None = None,
        key_prefix: str | None = None,
        device_id: str | None = None,
        read_only: bool = False,
    ) -> None:
        self._read_only = read_only
        self.state_path = state_path or DEFAULT_STATE_PATH
        self.redis_url = redis_url or os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)
        self.key_prefix = key_prefix or os.environ.get(
            "MILEAGE_KEY_PREFIX", DEFAULT_KEY_PREFIX
        )
        self.device_id = device_id or default_device_id()

        self.best: dict[str, float | None] = dict.fromkeys(RECORD_KINDS)
        self.today: dict[str, float | None] = dict.fromkeys(RECORD_KINDS)
        self.today_date = ""
        self.updated_at = ""
        self._pending = False
        self._redis: Any = None
        self._runs = RunTracker()

        if not read_only:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._merge_record(self._read_file())
        self._connect_redis()
        redis_data = self._read_redis()
        if redis_data:
            self._merge_record(redis_data)
        # The Pi has no RTC, but its restored clock is never ahead of real
        # time, so an older stored day is definitely stale. GPS days take
        # over from the first fix.
        if self.today_date and self.today_date < date.today().isoformat():
            self._start_day("")
        if not self.updated_at:
            self.updated_at = _utc_now()
        if not read_only:
            self.flush()

    def _redis_hash_name(self) -> str:
        return f"{self.key_prefix}:{REDIS_HASH_KEY}"

    def _connect_redis(self) -> None:
        if redis is None:
            return
        try:
            client = redis.from_url(self.redis_url, socket_connect_timeout=2, socket_timeout=2)
            client.ping()
            self._redis = client
        except (OSError, redis.RedisError, redis.ConnectionError):
            self._redis = None

    def _read_file(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {}
        try:
            data = json.loads(self.state_path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_file(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        with tmp.open("w") as handle:
            handle.write(json.dumps(self.snapshot(), indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self.state_path)
        directory = os.open(self.state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _read_redis(self) -> dict[str, Any]:
        if self._redis is None:
            return {}
        try:
            raw = self._redis.hgetall(self._redis_hash_name())
        except redis.RedisError:
            return {}
        out: dict[str, Any] = {}
        for key, val in (raw or {}).items():
            k = key.decode() if isinstance(key, bytes) else str(key)
            out[k] = val.decode() if isinstance(val, bytes) else val
        return out

    def _write_redis(self) -> None:
        if self._redis is None:
            return
        mapping = {
            key: "" if value is None else str(value)
            for key, value in self.snapshot().items()
        }
        try:
            self._redis.hset(self._redis_hash_name(), mapping=mapping)
        except redis.RedisError:
            self._redis = None

    @staticmethod
    def _seconds(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed > 0 else None

    def _merge_record(self, data: dict[str, Any]) -> None:
        """Keep the fastest times; today's only for the newest stored day."""
        if data.get("device_id"):
            self.device_id = str(data["device_id"])
        if str(data.get("timing_version") or "1") != str(TIMING_VERSION):
            # Older timing started at 5 mph, so its times are not comparable.
            return
        for kind in RECORD_KINDS:
            other = self._seconds(data.get(f"best_{kind}_sec"))
            if other is not None and (self.best[kind] is None or other < self.best[kind]):
                self.best[kind] = other
        other_day = str(data.get("today_date") or "")
        if other_day > self.today_date:
            self._start_day(other_day)
        if other_day and other_day == self.today_date:
            for kind in RECORD_KINDS:
                other = self._seconds(data.get(f"today_{kind}_sec"))
                if other is not None and (self.today[kind] is None or other < self.today[kind]):
                    self.today[kind] = other
        if str(data.get("updated_at") or "") > self.updated_at:
            self.updated_at = str(data["updated_at"])

    def _start_day(self, day: str) -> None:
        self.today_date = day
        self.today = dict.fromkeys(RECORD_KINDS)
        self._pending = True

    def snapshot(self) -> dict[str, Any]:
        snap: dict[str, Any] = {"device_id": self.device_id, "timing_version": TIMING_VERSION}
        for kind in RECORD_KINDS:
            snap[f"best_{kind}_sec"] = self.best[kind]
        snap["today_date"] = self.today_date
        for kind in RECORD_KINDS:
            snap[f"today_{kind}_sec"] = self.today[kind]
        snap["updated_at"] = self.updated_at
        return snap

    def invalidate_sample(self) -> None:
        """Never time a run across a lost fix, gap or serial reconnect."""
        self._runs.invalidate()

    def tick(
        self,
        mph: float | None,
        now: float | None,
        *,
        lat: float | None = None,
        lon: float | None = None,
        fix_ok: bool = False,
    ) -> list[NewRecord]:
        """Process one RMC fix at receiver UTC time; return any new records."""
        if not fix_ok or mph is None or now is None or lat is None or lon is None:
            self.invalidate_sample()
            return []
        try:
            day = local_day(now)
        except (OverflowError, OSError, ValueError):
            self.invalidate_sample()
            return []
        if day > self.today_date:
            self._start_day(day)
        new: list[NewRecord] = []
        for kind, seconds in self._runs.tick(mph, now, lat, lon):
            seconds = round(seconds, 2)
            scope = None
            if self.today[kind] is None or seconds < self.today[kind]:
                self.today[kind] = seconds
                scope = "today"
            if self.best[kind] is None or seconds < self.best[kind]:
                self.best[kind] = seconds
                scope = "best"
            if scope is not None:
                self._pending = True
                new.append(NewRecord(kind, seconds, scope))
        return new

    def reset(self) -> None:
        self.best = dict.fromkeys(RECORD_KINDS)
        self._start_day("")
        self.flush()

    def maybe_flush(self) -> bool:
        """Write only when something changed (spares the SD card)."""
        if not self._pending:
            return False
        self.flush()
        return True

    def flush(self) -> None:
        if self._read_only:
            raise RuntimeError("Cannot write a read-only records snapshot")
        self.updated_at = _utc_now()
        self._write_file()
        if self._redis is None:
            self._connect_redis()
        self._write_redis()
        self._pending = False


def load_snapshot(
    *,
    state_path: Path | None = None,
    redis_url: str | None = None,
    key_prefix: str | None = None,
) -> dict[str, Any]:
    store = PersonalRecordsStore(
        state_path=state_path,
        redis_url=redis_url,
        key_prefix=key_prefix,
        read_only=True,
    )
    return store.snapshot()
