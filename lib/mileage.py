"""GPS speed–based mileage with Redis + local JSON (upload / inspect later)."""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_device_id() -> str:
    return os.environ.get("MILEAGE_DEVICE_ID") or socket.gethostname()


class MileageStore:
    """Integrate speed (mph) over time; persist to JSON and optional Redis."""

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

        self.odometer_miles = 0.0
        self.trip_miles = 0.0
        self.updated_at = _utc_now()
        self._pending_miles = 0.0
        self._last_tick: float | None = None
        self._redis: Any = None

        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        file_data = self._read_file()
        self._apply_record(file_data)
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
            "updated_at": snap["updated_at"],
        }
        try:
            self._redis.hset(self._redis_hash_name(), mapping=mapping)
        except redis.RedisError:
            pass

    @staticmethod
    def _float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _apply_record(self, data: dict[str, Any]) -> None:
        if not data:
            return
        self.odometer_miles = self._float(data.get("odometer_miles"))
        self.trip_miles = self._float(data.get("trip_miles"))
        if data.get("updated_at"):
            self.updated_at = str(data["updated_at"])
        if data.get("device_id"):
            self.device_id = str(data["device_id"])

    def _merge_record(self, data: dict[str, Any]) -> None:
        """Keep the highest odometer; prefer newest trip from file on tie."""
        other_odo = self._float(data.get("odometer_miles"))
        if other_odo > self.odometer_miles:
            self.odometer_miles = other_odo
        other_trip = self._float(data.get("trip_miles"))
        other_at = str(data.get("updated_at") or "")
        if other_at > self.updated_at and other_trip > self.trip_miles:
            self.trip_miles = other_trip
        if data.get("device_id"):
            self.device_id = str(data["device_id"])

    def snapshot(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "odometer_miles": round(self.odometer_miles, 4),
            "trip_miles": round(self.trip_miles, 4),
            "updated_at": self.updated_at,
        }

    def tick(self, mph: float | None, now: float, *, min_mph: float = 1.0) -> None:
        if self._last_tick is None:
            self._last_tick = now
            return
        dt = now - self._last_tick
        self._last_tick = now
        if dt <= 0 or dt > 30:
            return
        if mph is None or mph < min_mph:
            return
        delta = mph * (dt / 3600.0)
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
        self.updated_at = _utc_now()
        self._write_file()
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
    )
    return store.snapshot()
