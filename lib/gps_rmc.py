"""Validate RMC samples and use receiver UTC rather than serial arrival time."""

import math
from datetime import datetime, timezone

KNOTS_TO_MPH = 1.1507794480235425


def parse_rmc_timestamp(line: str) -> float | None:
    try:
        parts = line.split('*', 1)[0].split(',')
        utc, date = parts[1], parts[9]
        if len(date) != 6 or len(utc.split('.')[0]) != 6:
            return None
        stamp = datetime.strptime(date + utc[:6], '%d%m%y%H%M%S').replace(tzinfo=timezone.utc)
        fraction = float('0' + utc[6:]) if utc[6:] else 0.0
        if not 0 <= fraction < 1:
            return None
        return stamp.timestamp() + fraction
    except (ValueError, IndexError, OverflowError):
        return None


def _degrees(raw: str, direction: str, limit: float) -> float:
    value = float(raw)
    degrees = int(value / 100)
    minutes = value - degrees * 100
    result = degrees + minutes / 60.0
    if not math.isfinite(result) or value < 0 or not 0 <= minutes < 60 or result > limit:
        raise ValueError('Invalid coordinates')
    return -result if direction in ('S', 'W') else result


def parse_rmc_line(line: str) -> tuple[bool, float | None, bool, tuple[float, float] | None]:
    """Return (is_rmc, mph, fix_ok, position); reject corrupt/incomplete fixes."""
    line = line.strip()
    if not line.startswith('$') or not line.split(',', 1)[0].endswith('RMC'):
        return False, None, False, None
    invalid = (True, None, False, None)
    try:
        body, checksum = line[1:].split('*')
        actual = 0
        for char in body:
            actual ^= ord(char)
        if len(checksum) != 2 or int(checksum, 16) != actual:
            return invalid
        parts = body.split(',')
        if len(parts) < 10 or parts[2] != 'A' or parse_rmc_timestamp(line) is None:
            return invalid
        # NMEA mode E/M/S/N is estimated/manual/simulated/invalid, not measured.
        if len(parts) > 12 and parts[12] and parts[12] not in ('A', 'D', 'F', 'R', 'P'):
            return invalid
        if parts[4] not in ('N', 'S') or parts[6] not in ('E', 'W'):
            return invalid
        lat = _degrees(parts[3], parts[4], 90)
        lon = _degrees(parts[5], parts[6], 180)
        knots = float(parts[7])
        if not math.isfinite(knots) or knots < 0:
            return invalid
        return True, knots * KNOTS_TO_MPH, True, (lat, lon)
    except (ValueError, IndexError, OverflowError):
        return invalid
