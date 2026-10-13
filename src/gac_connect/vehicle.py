"""Charge-control levers, built on the ``charging/reservation`` command.

The gateway has no plain "start" / "stop"; charging is gated through the schedule:

* **charge now** — ``{"chargingMode": 0}`` alone (free: charge whenever plugged
  in). The car keeps its saved window. The command returns immediately; the car
  applies it asynchronously and resuming can take several minutes.
* **resume schedule** — the last reported window, scheduled again.
* **pause** — unavailable until a continuous stop operation is verified.
* **set window** — loop mode (2) with times of day and calendar fields dated for
  the next (selected) occurrence. Selected days repeat; no days (``weekly`` 0) is
  a one-time window, which the car records as the app's "once" (mode 1). 127 is
  every day.

Times are car-local. ``weekly`` is a day bitmask (Mon=1, Tue=2, … Sun=64).

Gateway behaviour verified on an Aion UT (2026-10-13): ``{"chargingMode": 0}``
alone switches the schedule off and the car keeps its window; mode 0 with any
schedule field is refused with HTTP 400. Loop mode with times of day and dated
calendar fields is accepted with or without days. Mode 1 is refused with 13101,
whether its times are of day or Unix ms, so a reported once window (mode 1,
Unix-ms times encoding the calendar clock in UTC+8) is resent as loop without
days. ``chargingEndType`` 1 is refused with 13101.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .const import IOV_PREFIX, SERVICE_CLOCK
from .errors import CommandError

_RESERVATION = f"{IOV_PREFIX}/charging/reservation"
MODE_FREE = 0
MODE_SCHEDULED = 1   # the app's "once"
MODE_LOOP = 2        # the app's "loop"
END_AT_TIME = 0
END_AT_SOC = 1

PAUSE_UNSUPPORTED = (
    "Continuous charging pause is unavailable. The previous pause operation allowed "
    "charging daily from 03:00 to 03:30. Use an explicit charge window instead."
)


def _ms_of_day(hh: int, mm: int) -> int:
    return (hh * 60 + mm) * 60000


EVERY_DAY = 127


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
    """Free charging: the mode alone, as the gateway requires; the car keeps its saved window.

    ``until_soc`` adds ``chargingEndType`` 1, which has not been verified with free charging.
    """
    op: dict = {"chargingMode": MODE_FREE}
    if until_soc:
        op["chargingEndType"] = END_AT_SOC
    return op


_STRUCT_KEYS = ("year", "month", "day", "hour", "minute", "second", "millisecond")


def _exact(value: object) -> int | None:
    if type(value) is int:
        return value
    return int(value) if type(value) is float and value.is_integer() else None


def _next_window(sh: int, sm: int, eh: int, em: int, tz: ZoneInfo, weekly: int,
                 now: datetime | None) -> tuple[datetime, datetime]:
    """The next start on a selected day (any day for 0) and its stop strictly afterwards."""
    now = datetime.now(tz) if now is None else now.astimezone(tz)
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    if start <= now:
        start += timedelta(days=1)
    while weekly and not weekly & (1 << start.weekday()):
        start += timedelta(days=1)
    stop = start.replace(hour=eh, minute=em)
    if stop <= start:
        stop += timedelta(days=1)
    return start, stop


def _dated(parts: dict) -> bool:
    try:
        datetime(*(parts[name] for name in _STRUCT_KEYS[:5]))
    except (TypeError, ValueError, OverflowError):
        return False
    return True


def reported_window(charging: object) -> dict | None:
    """The reservation window fields of a ``chargingAccessors`` report, or None.

    Both daily times must be usable milliseconds-of-day or Unix-ms values, as the
    car reported them. Calendar structs are kept only when they form a real date
    (an Aion UT reports app-made repeating windows undated: year 2000, month and
    day 0).
    """
    if not isinstance(charging, dict):
        return None
    start, stop = (_exact(charging.get(key)) for key in ("dailyReservationStartTime", "dailyReservationStopTime"))
    if start is None or stop is None or not 0 <= start < 253402300800000 or not 0 <= stop < 253402300800000:
        return None
    weekly = _exact(charging.get("weeklyReservation"))
    end_type = _exact(charging.get("chargingEndType"))
    mode = _exact(charging.get("chargingMode"))
    window = {"dailyReservationStartTime": start, "dailyReservationStopTime": stop,
              "weeklyReservation": weekly if weekly is not None and 0 <= weekly <= 127 else 0,
              "chargingEndType": end_type if end_type in (END_AT_TIME, END_AT_SOC) else END_AT_TIME}
    for key in ("startTime", "stopTime"):
        struct = charging.get(key)
        parts = {name: _exact(struct.get(name)) for name in _STRUCT_KEYS} if isinstance(struct, dict) else None
        if parts and _dated(parts):
            window[key] = {name: part if part is not None else 0 for name, part in parts.items()}
    if mode in (MODE_SCHEDULED, MODE_LOOP):
        window["chargingMode"] = mode      # the scheduled mode to resume later
    return window


def _build(sh: int, sm: int, eh: int, em: int, tz: ZoneInfo, weekly: int, end_type: int,
           now: datetime | None) -> dict:
    """Loop mode with times of day, dated for the next occurrence (on a selected day, if any)."""
    start, stop = _next_window(sh, sm, eh, em, tz, weekly, now)
    return _operation(_ms_of_day(sh, sm), _ms_of_day(eh, em), weekly, MODE_LOOP, end_type,
                      start_struct=_struct(start), stop_struct=_struct(stop))


def _window_clock(window: dict) -> tuple[int, int, int, int]:
    """Start and stop clock (hour, minute) of a reported window."""
    start, stop = window["dailyReservationStartTime"], window["dailyReservationStopTime"]
    if start < 86_400_000 and stop < 86_400_000:
        return start // 3_600_000, start // 60_000 % 60, stop // 3_600_000, stop // 60_000 % 60
    first, last = window.get("startTime"), window.get("stopTime")
    if first and last:
        return first["hour"], first["minute"], last["hour"], last["minute"]
    begin, end = (datetime.fromtimestamp(value / 1000, SERVICE_CLOCK) for value in (start, stop))
    return begin.hour, begin.minute, end.hour, end.minute


def preserved_operation(window: dict, mode: int, *, until_soc: bool | None = None,
                        tz: ZoneInfo | None = None, now: datetime | None = None) -> dict:
    """Schedule a reported window again (optionally changing the stopping rule).

    The reported clock times are kept and rebuilt in loop mode, dated for the next
    occurrence in ``tz``: with the reported days for a repeating window, without
    days for a once window (``mode`` 1). Without ``tz`` the reported fields are
    resent as they are, in ``mode``.
    """
    end_type = window["chargingEndType"] if until_soc is None else END_AT_SOC if until_soc else END_AT_TIME
    if tz is not None:
        sh, sm, eh, em = _window_clock(window)
        if (sh, sm) != (eh, em):
            weekly = 0 if mode == MODE_SCHEDULED else window["weeklyReservation"]
            return _build(sh, sm, eh, em, tz, weekly, end_type, now)
    return _operation(window["dailyReservationStartTime"], window["dailyReservationStopTime"],
                      window["weeklyReservation"], mode, end_type,
                      start_struct=window.get("startTime"), stop_struct=window.get("stopTime"))


def pause_operation(tz: ZoneInfo) -> dict:
    """Reject the retired pseudo-pause; retain the API for a clear migration error."""
    raise CommandError(PAUSE_UNSUPPORTED)


def validate_clock(value: str) -> str:
    """Validate a car-local HH:MM value before constructing a charge command."""
    if not isinstance(value, str) or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) is None:
        raise ValueError("charge times must use HH:MM, from 00:00 to 23:59")
    return value


def validate_weekly(weekly: int | float) -> int:
    """Validate a Mon=1 through Sun=64 day mask; zero means once (the next occurrence only)."""
    if (isinstance(weekly, bool) or not isinstance(weekly, (int, float))
            or not 0 <= weekly <= 127 or weekly != int(weekly)):
        raise ValueError("weekly must be an integer from 0 to 127")
    return int(weekly)


def window_operation(start_hhmm: str, stop_hhmm: str, tz: ZoneInfo, *,
                     weekly: int = 0, until_soc: bool = False, now: datetime | None = None) -> dict:
    """Build a charge window: selected days repeat, 127 is every day, and 0 is one time (the app's Once).

    The calendar fields are dated for the next (selected) occurrence, with the
    stop strictly after the start.
    """
    validate_clock(start_hhmm)
    validate_clock(stop_hhmm)
    weekly = validate_weekly(weekly)
    if start_hhmm == stop_hhmm:
        raise ValueError("start and stop must differ; use Charge now for unrestricted charging")
    if not isinstance(until_soc, bool):
        raise ValueError("until_soc must be a boolean")
    sh, sm = map(int, start_hhmm.split(":"))
    eh, em = map(int, stop_hhmm.split(":"))
    return _build(sh, sm, eh, em, tz, weekly, END_AT_SOC if until_soc else END_AT_TIME, now)
