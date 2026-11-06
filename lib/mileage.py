"""GPS mileage from position deltas while driving (Redis + local JSON)."""

from __future__ import annotations

import json
import math
import os
import socket
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

try:
    import redis
except ImportError:  # pragma: no cover
    redis = None  # type: ignore[assignment,misc]

DEFAULT_STATE_PATH = Path("/var/lib/automotive_gps/odometer.json")
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
DEFAULT_KEY_PREFIX = "automotive_gps"
REDIS_HASH_KEY = "mileage"

# Parked GPS often reports 2–5 mph; integrating speed caused false miles indoors.
EARTH_RADIUS_MI = 3958.7613
DEFAULT_DRIVING_ENTER_MPH = 4.0
DEFAULT_DRIVING_ENTER_SEC = 3.0
DEFAULT_PARKED_MAX_MPH = 2.0
DEFAULT_PARKED_HOLD_SEC = 45.0
DEFAULT_MAX_SEGMENT_MI = 0.12
DEFAULT_MAX_IMPLIED_MPH = 120.0
MAX_SAMPLE_GAP_SEC = 1.5


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def local_day(timestamp: float) -> str:
    """Calendar day (Pi's local time zone) of a GPS UTC timestamp."""
    return datetime.fromtimestamp(timestamp).date().isoformat()


def default_device_id() -> str:
    return os.environ.get("MILEAGE_DEVICE_ID") or socket.gethostname()


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
        return value if math.isfinite(value) and value >= 0 else default
    except ValueError:
        return default


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles."""
    rlat1, rlon1, rlat2, rlon2 = (
        math.radians(lat1),
        math.radians(lon1),
        math.radians(lat2),
        math.radians(lon2),
    )
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(
        dlon / 2
    ) ** 2
    c = 2 * math.asin(min(1.0, math.sqrt(a)))
    return EARTH_RADIUS_MI * c


class MileageStore:
    """Accumulate validated GPS position steps while driving; persist totals."""

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

        self.odometer_miles = 0.0
        self.trip_miles = 0.0
        self.trip_date = ""
        self.updated_at = ""
        self._pending_miles = 0.0
        self._last_tick: float | None = None
        self._last_mph: float | None = None
        self._candidate_miles = 0.0
        self._last_pos: tuple[float, float] | None = None
        self._driving = False
        self._fast_since: float | None = None
        self._slow_since: float | None = None
        self._redis: Any = None

        if not read_only:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
        file_data = self._read_file()
        self._apply_record(file_data)
        self._connect_redis()
        redis_data = self._read_redis()
        if redis_data:
            self._merge_record(redis_data)
        # TRIP is per day. The Pi's restored clock is never ahead of real
        # time, so an older trip day is definitely over; GPS days take over
        # from the first fix.
        if self.trip_date and self.trip_date < date.today().isoformat():
            self.trip_miles = 0.0
            self.trip_date = ""
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
        payload = self.snapshot()
        tmp = self.state_path.with_suffix(".tmp")
        with tmp.open("w") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")
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
        if not raw:
            return {}
        decoded: dict[str, Any] = {}
        for key, val in raw.items():
            k = key.decode() if isinstance(key, bytes) else str(key)
            v = val.decode() if isinstance(val, bytes) else val
            decoded[k] = v
        return decoded

    def _write_redis(self) -> None:
        if self._redis is None:
            return
        snap = self.snapshot()
        mapping = {
            "device_id": snap["device_id"],
            "odometer_miles": str(snap["odometer_miles"]),
            "trip_miles": str(snap["trip_miles"]),
            "trip_date": snap["trip_date"],
            "updated_at": snap["updated_at"],
        }
        try:
            self._redis.hset(self._redis_hash_name(), mapping=mapping)
        except redis.RedisError:
            self._redis = None

    @staticmethod
    def _float(value: Any, default: float = 0.0) -> float:
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) and parsed >= 0 else default
        except (TypeError, ValueError):
            return default

    def _apply_record(self, data: dict[str, Any]) -> None:
        if not data:
            return
        self.odometer_miles = self._float(data.get("odometer_miles"))
        self.trip_miles = self._float(data.get("trip_miles"))
        self.trip_date = str(data.get("trip_date") or "")
        if data.get("updated_at"):
            self.updated_at = str(data["updated_at"])
        if data.get("device_id"):
            self.device_id = str(data["device_id"])

    def _merge_record(self, data: dict[str, Any]) -> None:
        """Keep the highest odometer; use the newest trip (which may have been reset)."""
        other_odo = self._float(data.get("odometer_miles"))
        if other_odo > self.odometer_miles:
            self.odometer_miles = other_odo
        other_trip = self._float(data.get("trip_miles"))
        other_day = str(data.get("trip_date") or "")
        other_at = str(data.get("updated_at") or "")
        if other_day > self.trip_date or (other_day == self.trip_date and other_at > self.updated_at):
            self.trip_miles = other_trip
            self.trip_date = other_day
        if other_at > self.updated_at:
            self.updated_at = other_at
        if data.get("device_id"):
            self.device_id = str(data["device_id"])

    def snapshot(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "odometer_miles": round(self.odometer_miles, 4),
            "trip_miles": round(self.trip_miles, 4),
            "trip_date": self.trip_date,
            "updated_at": self.updated_at,
        }

    def invalidate_sample(self) -> None:
        """Never bridge a lost fix, rejected step, or serial reconnect."""
        self._last_tick = None
        self._last_pos = None
        self._last_mph = None
        self._driving = False
        self._fast_since = None
        self._slow_since = None
        self._candidate_miles = 0.0

    def tick(
        self,
        mph: float | None,
        now: float,
        *,
        lat: float | None = None,
        lon: float | None = None,
        fix_ok: bool = False,
    ) -> None:
        """Process one RMC fix at receiver UTC time, never its arrival time."""
        enter_mph = _env_float("MILEAGE_DRIVING_ENTER_MPH", DEFAULT_DRIVING_ENTER_MPH)
        enter_sec = _env_float("MILEAGE_DRIVING_ENTER_SEC", DEFAULT_DRIVING_ENTER_SEC)
        parked_mph = _env_float("MILEAGE_PARKED_MAX_MPH", DEFAULT_PARKED_MAX_MPH)
        parked_sec = _env_float("MILEAGE_PARKED_HOLD_SEC", DEFAULT_PARKED_HOLD_SEC)
        max_seg = _env_float("MILEAGE_MAX_SEGMENT_MI", DEFAULT_MAX_SEGMENT_MI)
        max_implied = _env_float("MILEAGE_MAX_IMPLIED_MPH", DEFAULT_MAX_IMPLIED_MPH)
        if (
            not fix_ok or mph is None or lat is None or lon is None
            or not all(math.isfinite(v) for v in (mph, now, lat, lon))
            or not 0 <= mph <= max_implied
            or not -90 <= lat <= 90 or not -180 <= lon <= 180
        ):
            self.invalidate_sample()
            return
        try:
            day = local_day(now)
        except (OverflowError, OSError, ValueError):
            self.invalidate_sample()
            return
        if day > self.trip_date:
            # First fix of a new day starts a new trip; the odometer keeps going.
            if self.trip_date:
                self.trip_miles = 0.0
            self.trip_date = day

        delta = 0.0
        if self._last_tick is not None:
            dt = now - self._last_tick
            if dt <= 0:
                self.invalidate_sample()
                return
            if dt > MAX_SAMPLE_GAP_SEC:
                self.invalidate_sample()
            else:
                delta = haversine_miles(*self._last_pos, lat, lon)
                implied = delta * 3600.0 / dt
                average_speed = (self._last_mph + mph) * 0.5
                # Reject position jumps inconsistent with receiver speed too.
                if (
                    delta > max_seg or implied > max_implied
                    or implied > max(average_speed * 3, average_speed + 15)
                ):
                    self.invalidate_sample()
                    return
        self._last_tick = now
        self._last_pos = (lat, lon)
        self._last_mph = mph

        if mph >= enter_mph:
            self._slow_since = None
            if not self._driving:
                if self._fast_since is None:
                    self._fast_since = now
                    self._candidate_miles = 0.0
                else:
                    self._candidate_miles += delta
                if now - self._fast_since >= enter_sec:
                    self._driving = True
                    # Credit the confirmation window instead of dropping it.
                    self._add_miles(self._candidate_miles)
                    self._candidate_miles = 0.0
                return
        else:
            self._fast_since = None
            self._candidate_miles = 0.0
            if self._slow_since is None:
                self._slow_since = now
            if now - self._slow_since >= parked_sec:
                self._driving = False

        # Suppress low-speed jitter immediately, even during the parked hold.
        # Unchanged coordinates still advance the stop/drive timers above.
        if self._driving and mph > parked_mph:
            self._add_miles(delta)

    def _add_miles(self, delta: float) -> None:
        if not math.isfinite(delta) or delta <= 0.0:
            return
        self.odometer_miles += delta
        self.trip_miles += delta
        self._pending_miles += delta

    def maybe_flush(
        self,
        now: float,
        last_flush: float,
        *,
        interval_sec: float = 30.0,
        min_miles: float = 0.02,
    ) -> bool:
        due_time = (now - last_flush) >= interval_sec
        due_miles = self._pending_miles >= min_miles
        if not due_time and not due_miles:
            return False
        self.flush()
        return True

    def flush(self) -> None:
        if self._read_only:
            raise RuntimeError("Cannot write a read-only mileage snapshot")
        self.updated_at = _utc_now()
        self._write_file()
        if self._redis is None:
            self._connect_redis()
        self._write_redis()
        self._pending_miles = 0.0

    def reset_trip(self) -> None:
        self.trip_miles = 0.0
        self.flush()


def load_store_from_env() -> MileageStore | None:
    if os.environ.get("MILEAGE_DISABLE", "").lower() in ("1", "true", "yes"):
        return None
    return MileageStore()


def load_snapshot(
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    redis_url: str | None = None,
    key_prefix: str | None = None,
) -> dict[str, Any]:
    """Read mileage for CLI without starting the GPS service."""
    store = MileageStore(
        state_path=state_path,
        redis_url=redis_url,
        key_prefix=key_prefix,
        read_only=True,
    )
    return store.snapshot()
