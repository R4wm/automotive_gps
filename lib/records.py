"""Personal driving records: 1/8 mi, 0–60, 20–60, top speed (Redis + JSON)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mileage import (  # same install dir on Pi
    DEFAULT_KEY_PREFIX,
    DEFAULT_REDIS_URL,
    _utc_now,
    default_device_id,
    haversine_miles,
    redis,
)

DEFAULT_STATE_PATH = Path("/var/lib/automotive_gps/records.json")
REDIS_HASH_KEY = "records"

RUN_END_MPH = 60.0
RUN_START_MPH = 1.0
RUN_20_MPH = 20.0
EIGHTH_MILE_MI = 0.125
EIGHTH_MIN_SEC = 4.0
EIGHTH_MAX_SEC = 45.0
EIGHTH_ABORT_MPH = 2.0
EIGHTH_MAX_SEGMENT_MI = 0.08


class EighthMileTracker:
    """Time from launch to 1/8 mile (0.125 mi) using GPS position deltas."""

    def __init__(self) -> None:
        self._start: float | None = None
        self._dist_mi = 0.0
        self._last_pos: tuple[float, float] | None = None

    def _reset(self) -> None:
        self._start = None
        self._dist_mi = 0.0
        self._last_pos = None

    def tick(
        self,
        mph: float,
        now: float,
        lat: float | None,
        lon: float | None,
    ) -> float | None:
        if mph < 0.5:
            self._reset()
            return None

        if lat is None or lon is None:
            return None

        if self._start is None:
            if mph < RUN_START_MPH:
                return None
            self._start = now
            self._last_pos = (lat, lon)
            self._dist_mi = 0.0
            return None

        elapsed = now - self._start
        if elapsed > EIGHTH_MAX_SEC:
            self._reset()
            return None

        if mph < EIGHTH_ABORT_MPH and self._dist_mi < EIGHTH_MILE_MI * 0.5:
            self._reset()
            return None

        if self._last_pos is not None:
            delta = haversine_miles(self._last_pos[0], self._last_pos[1], lat, lon)
            if delta > EIGHTH_MAX_SEGMENT_MI:
                self._reset()
                return None
            self._dist_mi += delta
        self._last_pos = (lat, lon)

        if self._dist_mi < EIGHTH_MILE_MI:
            return None

        if elapsed < EIGHTH_MIN_SEC:
            self._reset()
            return None

        self._reset()
        return elapsed


class AccelerationTracker:
    """Detect completed 0–60 and 20–60 runs from speed samples."""

    def __init__(self) -> None:
        self._zero_sixty_start: float | None = None
        self._twenty_sixty_start: float | None = None
        self._below_20 = True

    def tick(self, mph: float, now: float) -> list[tuple[str, float]]:
        completed: list[tuple[str, float]] = []

        if mph < 0.5:
            self._zero_sixty_start = None
        elif mph < RUN_END_MPH:
            if self._zero_sixty_start is None and mph >= RUN_START_MPH:
                self._zero_sixty_start = now
        else:
            if self._zero_sixty_start is not None:
                elapsed = now - self._zero_sixty_start
                if 2.0 <= elapsed <= 120.0:
                    completed.append(("0-60", elapsed))
            self._zero_sixty_start = None

        if mph < RUN_20_MPH - 1.0:
            self._below_20 = True
            self._twenty_sixty_start = None
        elif mph < RUN_END_MPH:
            if self._below_20 and mph >= RUN_20_MPH:
                self._twenty_sixty_start = now
                self._below_20 = False
        else:
            if self._twenty_sixty_start is not None:
                elapsed = now - self._twenty_sixty_start
                if 1.0 <= elapsed <= 90.0:
                    completed.append(("20-60", elapsed))
            self._twenty_sixty_start = None
            self._below_20 = mph < RUN_20_MPH

        return completed


class PersonalRecordsStore:
    def __init__(
        self,
        *,
        redis_url: str | None = None,
        state_path: Path | None = None,
        key_prefix: str | None = None,
        device_id: str | None = None,
    ) -> None:
        self.state_path = state_path or DEFAULT_STATE_PATH
        self.redis_url = redis_url or os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)
        self.key_prefix = key_prefix or os.environ.get(
            "MILEAGE_KEY_PREFIX", DEFAULT_KEY_PREFIX
        )
        self.device_id = device_id or default_device_id()

        self.best_eighth_mile_sec: float | None = None
        self.best_zero_sixty_sec: float | None = None
        self.best_twenty_sixty_sec: float | None = None
        self.top_speed_mph: float = 0.0
        self.updated_at = _utc_now()
        self._pending = False
        self._redis: Any = None
        self._tracker = AccelerationTracker()
        self._eighth = EighthMileTracker()

        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._apply_record(self._read_file())
        self._connect_redis()
        redis_data = self._read_redis()
        if redis_data:
            self._merge_record(redis_data)
        self.flush()

    def _redis_hash_name(self) -> str:
        return f"{self.key_prefix}:{REDIS_HASH_KEY}"

    def _connect_redis(self) -> None:
        if redis is None:
            return
        try:
            client = redis.from_url(self.redis_url, socket_connect_timeout=2)
            client.ping()
            self._redis = client
        except (OSError, redis.RedisError, redis.ConnectionError):
            self._redis = None

    def _read_file(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {}
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_file(self) -> None:
        payload = self.snapshot()
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        tmp.replace(self.state_path)

    def _read_redis(self) -> dict[str, Any]:
        if self._redis is None:
            return {}
        try:
            raw = self._redis.hgetall(self._redis_hash_name())
        except redis.RedisError:
            return {}
        if not raw:
            return {}
        out: dict[str, Any] = {}
        for key, val in raw.items():
            k = key.decode() if isinstance(key, bytes) else str(key)
            v = val.decode() if isinstance(val, bytes) else val
            out[k] = v
        return out

    def _write_redis(self) -> None:
        if self._redis is None:
            return
        snap = self.snapshot()
        mapping = {
            "device_id": snap["device_id"],
            "best_eighth_mile_sec": ""
            if snap["best_eighth_mile_sec"] is None
            else str(snap["best_eighth_mile_sec"]),
            "best_zero_sixty_sec": ""
            if snap["best_zero_sixty_sec"] is None
            else str(snap["best_zero_sixty_sec"]),
            "best_twenty_sixty_sec": ""
            if snap["best_twenty_sixty_sec"] is None
            else str(snap["best_twenty_sixty_sec"]),
            "top_speed_mph": str(snap["top_speed_mph"]),
            "updated_at": snap["updated_at"],
        }
        try:
            self._redis.hset(self._redis_hash_name(), mapping=mapping)
        except redis.RedisError:
            pass

    @staticmethod
    def _float(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _apply_record(self, data: dict[str, Any]) -> None:
        if not data:
            return
        e = self._float(data.get("best_eighth_mile_sec"))
        z = self._float(data.get("best_zero_sixty_sec"))
        t = self._float(data.get("best_twenty_sixty_sec"))
        if e is not None:
            self.best_eighth_mile_sec = e
        if z is not None:
            self.best_zero_sixty_sec = z
        if t is not None:
            self.best_twenty_sixty_sec = t
        top = self._float(data.get("top_speed_mph"))
        if top is not None:
            self.top_speed_mph = top
        if data.get("updated_at"):
            self.updated_at = str(data["updated_at"])
        if data.get("device_id"):
            self.device_id = str(data["device_id"])

    def _merge_record(self, data: dict[str, Any]) -> None:
        e = self._float(data.get("best_eighth_mile_sec"))
        z = self._float(data.get("best_zero_sixty_sec"))
        t = self._float(data.get("best_twenty_sixty_sec"))
        if e is not None and (
            self.best_eighth_mile_sec is None or e < self.best_eighth_mile_sec
        ):
            self.best_eighth_mile_sec = e
        if z is not None and (
            self.best_zero_sixty_sec is None or z < self.best_zero_sixty_sec
        ):
            self.best_zero_sixty_sec = z
        if t is not None and (
            self.best_twenty_sixty_sec is None or t < self.best_twenty_sixty_sec
        ):
            self.best_twenty_sixty_sec = t
        top = self._float(data.get("top_speed_mph"))
        if top is not None and top > self.top_speed_mph:
            self.top_speed_mph = top

    def snapshot(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "best_eighth_mile_sec": self.best_eighth_mile_sec,
            "best_zero_sixty_sec": self.best_zero_sixty_sec,
            "best_twenty_sixty_sec": self.best_twenty_sixty_sec,
            "top_speed_mph": round(self.top_speed_mph, 2),
            "updated_at": self.updated_at,
        }

    def note_run(self, kind: str, seconds: float) -> bool:
        improved = False
        if kind == "1/8":
            if self.best_eighth_mile_sec is None or seconds < self.best_eighth_mile_sec:
                self.best_eighth_mile_sec = round(seconds, 3)
                improved = True
        elif kind == "0-60":
            if self.best_zero_sixty_sec is None or seconds < self.best_zero_sixty_sec:
                self.best_zero_sixty_sec = round(seconds, 3)
                improved = True
        elif kind == "20-60":
            if self.best_twenty_sixty_sec is None or seconds < self.best_twenty_sixty_sec:
                self.best_twenty_sixty_sec = round(seconds, 3)
                improved = True
        if improved:
            self._pending = True
        return improved

    def tick_speed(
        self,
        mph: float,
        now: float,
        *,
        fix_ok: bool,
        lat: float | None = None,
        lon: float | None = None,
    ) -> None:
        if not fix_ok:
            return
        if mph > self.top_speed_mph:
            self.top_speed_mph = mph
            self._pending = True
        eighth = self._eighth.tick(mph, now, lat, lon)
        if eighth is not None:
            self.note_run("1/8", eighth)
        for kind, elapsed in self._tracker.tick(mph, now):
            self.note_run(kind, elapsed)

    def maybe_flush(self, now: float, last_flush: float, interval_sec: float = 30.0) -> bool:
        if not self._pending and (now - last_flush) < interval_sec:
            return False
        self.flush()
        return True

    def flush(self) -> None:
        self.updated_at = _utc_now()
        self._write_file()
        self._write_redis()
        self._pending = False
