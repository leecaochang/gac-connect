"""Charge-control levers, built on the ``charging/reservation`` command.

The gateway has no plain "start" / "stop"; charging is gated through the schedule:

* **charge now** — mode 0 (free): charge whenever plugged in. The command
  returns immediately; the car applies it asynchronously and resuming can take
  several minutes.
* **pause** — unavailable until a continuous stop operation is verified.
* **set window** — mode 1 with a real daily window.

Times are car-local. ``weekly`` is a day bitmask (Mon=1, Tue=2, … Sun=64; 0 = every day).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .const import IOV_PREFIX
from .errors import CommandError

_RESERVATION = f"{IOV_PREFIX}/charging/reservation"
MODE_FREE = 0
MODE_SCHEDULED = 1
END_AT_TIME = 0
END_AT_SOC = 1

PAUSE_UNSUPPORTED = (
    "Continuous charging pause is unavailable. The previous pause operation allowed "
    "charging daily from 03:00 to 03:30. Use an explicit charge window instead."
)


def _ms_of_day(hh: int, mm: int) -> int:
    return (hh * 60 + mm) * 60000


def _struct(dt: datetime) -> dict:
    return {"year": dt.year, "month": dt.month, "day": dt.day,
            "hour": dt.hour, "minute": dt.minute, "second": 0, "millisecond": 0}


def _operation(start_ms: int, stop_ms: int, weekly: int, mode: int, end_type: int,
               start_struct: dict | None = None, stop_struct: dict | None = None) -> dict:
    op = {
        "dailyReservationStartTime": start_ms,
        "dailyReservationStopTime": stop_ms,
        "weeklyReservation": weekly,
        "chargingMode": mode,
        "chargingEndType": end_type,
    }
    if start_struct is not None:
        op["startTime"] = start_struct
    if stop_struct is not None:
        op["stopTime"] = stop_struct
    return op


def reservation_body(vin: str, operation: dict) -> dict:
    return {"identifier": {"vin": vin}, "operations": [operation]}


def charge_now_operation(*, until_soc: bool = False) -> dict:
    return _operation(_ms_of_day(6, 0), _ms_of_day(8, 0), 0, MODE_FREE,
                      END_AT_SOC if until_soc else END_AT_TIME)


def pause_operation(tz: ZoneInfo) -> dict:
    """Reject the retired pseudo-pause; retain the API for a clear migration error."""
    raise CommandError(PAUSE_UNSUPPORTED)


def validate_clock(value: str) -> str:
    """Validate a car-local HH:MM value before constructing a charge command."""
    if not isinstance(value, str) or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) is None:
        raise ValueError("charge times must use HH:MM, from 00:00 to 23:59")
    return value


def validate_weekly(weekly: int | float) -> int:
    """Validate a Mon=1 through Sun=64 day mask; zero means every day."""
    if (isinstance(weekly, bool) or not isinstance(weekly, (int, float))
            or not 0 <= weekly <= 127 or weekly != int(weekly)):
        raise ValueError("weekly must be an integer from 0 to 127")
    return int(weekly)


def window_operation(start_hhmm: str, stop_hhmm: str, tz: ZoneInfo, *,
                     weekly: int = 0, until_soc: bool = False, now: datetime | None = None,
                     timestamp_times: bool = False) -> dict:
    """Schedule the next selected start day, with its stop strictly afterwards."""
    validate_clock(start_hhmm)
    validate_clock(stop_hhmm)
    weekly = validate_weekly(weekly)
    if start_hhmm == stop_hhmm:
        raise ValueError("start and stop must differ; use Charge now for unrestricted charging")
    if not isinstance(until_soc, bool):
        raise ValueError("until_soc must be a boolean")
    sh, sm = map(int, start_hhmm.split(":"))
    eh, em = map(int, stop_hhmm.split(":"))
    now = datetime.now(tz) if now is None else now.astimezone(tz)
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    if start <= now:
        start += timedelta(days=1)
    while weekly and not weekly & (1 << start.weekday()):
        start += timedelta(days=1)
    stop = start.replace(hour=eh, minute=em)
    if stop <= start:
        stop += timedelta(days=1)
    return _operation(
        int(start.timestamp() * 1000) if timestamp_times else _ms_of_day(sh, sm),
        int(stop.timestamp() * 1000) if timestamp_times else _ms_of_day(eh, em), weekly, MODE_SCHEDULED,
        END_AT_SOC if until_soc else END_AT_TIME,
        start_struct=_struct(start), stop_struct=_struct(stop),
    )
