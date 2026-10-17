"""Charge-control levers, built on the ``charging/reservation`` command.

The gateway has no plain "start" / "stop"; charging is gated through the schedule:

* **charge now** — ``{"chargingMode": 0}`` alone (free: charge whenever plugged
  in). The car keeps its saved window. The command returns immediately; the car
  applies it asynchronously and resuming can take several minutes. It does not
  start a new charge once one has ended (``models.restart_needed``: a window finished,
  or the connector was unlocked): a one-time window to the charge limit starting at
  the next minute does (``restart_operation``), and charge now sent once charging has
  begun keeps it going past that window (Aion UT, 2026-10-17).
* **resume schedule** — the last reported window, scheduled again. This is also
  the only pause: the app has none, and with a schedule on the car blocks AC
  charging outside the window (DC fast charging starts regardless). A one-time
  window far ahead is acknowledged but not kept, so there is no open-ended pause.
* **set window** — selected days repeat in loop mode (2), with times of day and
  calendar fields dated for the next occurrence; 127 is every day. No days (``weekly``
  0) is a one-time window, sent as the app's "once": mode 1 with dated calendar fields
  only. The car never starts a one-time window whose start has passed, so one already
  open is sent starting in a minute. After a one-time window the car reports once mode
  with both times 0 (the app's "Charging time is not set") and waits.

Times are car-local. ``weekly`` is a day bitmask (Mon=1, Tue=2, … Sun=64).

Gateway behaviour verified on an Aion UT (2026-10-13): ``{"chargingMode": 0}``
alone switches the schedule off and the car keeps its window; mode 0 with any
schedule field is refused with HTTP 400. Loop mode with times of day and dated
calendar fields is accepted with or without days. Mode 1 with times (of day or
Unix ms) is refused with 13101, so a reported once window (mode 1, Unix-ms times
encoding the calendar clock in UTC+8) is resent as loop without days. Stopping at
the car's charge limit (``chargingEndType`` 1) sends no stop time, as the app does;
with one the request is refused with 13101. The car keeps its saved stop time. One
time to the charge limit is sent as the app's once: mode 1 with a dated start alone
(verified 2026-10-16: the car waited and started on time); one time to a stop time is
mode 1 with a dated start and stop (verified 2026-10-17: started and stopped on time).
Loop mode without days was accepted for one time, but the car then kept only undated
hours and did not start it. A window that crosses midnight sends its stop as the time of
day plus 24 hours (00:10 as 24:10), as the app does and as the car reports it; a stop
earlier than the start is refused with 13002.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .const import IOV_PREFIX, SERVICE_CLOCK
from .models import undated_window

_RESERVATION = f"{IOV_PREFIX}/charging/reservation"
MODE_FREE = 0
MODE_SCHEDULED = 1   # the app's "once"
MODE_LOOP = 2        # the app's "loop"
END_AT_TIME = 0
END_AT_SOC = 1


_DAY_MS = 86_400_000


class WindowClosingError(ValueError):
    """A one-time window that ends before the car could start it."""


def _ms_of_day(hh: int, mm: int) -> int:
    return (hh * 60 + mm) * 60000


EVERY_DAY = 127


def _struct(dt: datetime) -> dict:
    return {"year": dt.year, "month": dt.month, "day": dt.day,
            "hour": dt.hour, "minute": dt.minute, "second": 0, "millisecond": 0}


def _operation(start_ms: int, stop_ms: int, weekly: int, mode: int, end_type: int,
               start_struct: dict | None = None, stop_struct: dict | None = None) -> dict:
    """A reservation; stopping at the charge limit leaves the stop time out, as the app does."""
    op = {"dailyReservationStartTime": start_ms, "weeklyReservation": weekly,
          "chargingMode": mode, "chargingEndType": end_type}
    if end_type != END_AT_SOC:
        op["dailyReservationStopTime"] = stop_ms
    if start_struct is not None:
        op["startTime"] = start_struct
    if stop_struct is not None and end_type != END_AT_SOC:
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
                 now: datetime | None, *, open_now: bool = True) -> tuple[datetime, datetime]:
    """The window on a selected day (any day for 0) that is open now, else the next one to start.

    Its stop is strictly after its start. Without ``open_now`` (no stop time is sent,
    so the car cannot tell whether a window is still open) only a start still ahead counts.
    """
    now = datetime.now(tz) if now is None else now.astimezone(tz)
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0) - timedelta(days=1)
    while True:
        stop = start.replace(hour=eh, minute=em)
        if stop <= start:
            stop += timedelta(days=1)
        if (not weekly or weekly & (1 << start.weekday())) and (stop > now if open_now else start > now):
            return start, stop
        start += timedelta(days=1)


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
    if start == stop == 0 and "startTime" not in window and "stopTime" not in window:
        if (undated := undated_window(charging)) is None:
            return None     # the car's "Charging time is not set": no window to resume
        (sh, sm), (eh, em) = undated     # kept only in the undated calendar fields
        window["dailyReservationStartTime"] = _ms_of_day(sh, sm)
        window["dailyReservationStopTime"] = _ms_of_day(eh, em) + (_DAY_MS if (eh, em) <= (sh, sm) else 0)
    if mode in (MODE_SCHEDULED, MODE_LOOP):
        window["chargingMode"] = mode      # the scheduled mode to resume later
    return window


def _next_minute(tz: ZoneInfo, now: datetime | None) -> datetime:
    """The next whole minute at least 30 s ahead: the car never starts a start already past."""
    current = datetime.now(tz) if now is None else now.astimezone(tz)
    return (current + timedelta(seconds=90)).replace(second=0, microsecond=0)


def restart_operation(tz: ZoneInfo, now: datetime | None = None) -> dict:
    """Start a new charge once one has ended: one time to the charge limit from the next minute."""
    return {"chargingMode": MODE_SCHEDULED, "chargingEndType": END_AT_SOC, "startTime": _struct(_next_minute(tz, now))}


def _build(sh: int, sm: int, eh: int, em: int, tz: ZoneInfo, weekly: int, end_type: int,
           now: datetime | None, *, open_now: bool = True) -> dict:
    """Loop mode with times of day, dated for the occurrence open now or else the next one
    (on a selected day, if any), so a one-time window already open starts at once.

    A stop at or before the start is the next day's, sent as its time of day plus 24 h
    (00:10 as 24:10), as the app does.
    """
    if end_type == END_AT_SOC and not weekly:
        # One time to the charge limit is the app's once: mode 1 with a dated start alone.
        # An Aion UT (2026-10-16) stopped charging at once, waited (chargingStatus 27) and
        # started on time; with a stop it refuses 13101.
        start, _ = _next_window(sh, sm, eh, em, tz, 0, now, open_now=False)
        return {"chargingMode": MODE_SCHEDULED, "chargingEndType": END_AT_SOC, "startTime": _struct(start)}
    if not weekly:
        # One time to a stop time: the app's once, dated start and stop (Aion UT, 2026-10-17:
        # started and stopped on time). A start already past is never started, so a window
        # open now starts at the next minute (at least 30 s ahead).
        start, stop = _next_window(sh, sm, eh, em, tz, 0, now, open_now=True)
        start = max(start, _next_minute(tz, now))
        if start >= stop:
            raise WindowClosingError("the window ends before the car could start it (a start already past "
                                     "is sent from the next minute); choose a later stop time")
        return {"chargingMode": MODE_SCHEDULED, "chargingEndType": END_AT_TIME,
                "startTime": _struct(start), "stopTime": _struct(stop)}
    start, stop = _next_window(sh, sm, eh, em, tz, weekly, now, open_now=open_now)
    stop_ms = _ms_of_day(eh, em) + (_DAY_MS if (eh, em) <= (sh, sm) else 0)
    return _operation(_ms_of_day(sh, sm), stop_ms, weekly, MODE_LOOP, end_type,
                      start_struct=_struct(start), stop_struct=_struct(stop))


def _window_clock(window: dict) -> tuple[int, int, int, int]:
    """Start and stop clock (hour, minute) of a reported window."""
    start, stop = window["dailyReservationStartTime"], window["dailyReservationStopTime"]
    if start < _DAY_MS and stop < 2 * _DAY_MS:      # a stop past midnight reads as 24:10 and so on
        return start // 3_600_000, start // 60_000 % 60, stop // 3_600_000 % 24, stop // 60_000 % 60
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


def window_operation(start_hhmm: str, stop_hhmm: str | None, tz: ZoneInfo, *,
                     weekly: int = 0, until_soc: bool = False, now: datetime | None = None) -> dict:
    """Build a charge window: selected days repeat, 127 is every day, and 0 is one time (the app's Once).

    The calendar fields are dated for the (selected) occurrence open now, else the
    next one, with the stop strictly after the start. With ``until_soc`` the car stops
    at its charge limit and no stop time is sent, so ``stop_hhmm`` may be None and only
    a start still ahead counts.
    """
    if not isinstance(until_soc, bool):
        raise ValueError("until_soc must be a boolean")
    validate_clock(start_hhmm)
    if stop_hhmm is None and until_soc:
        stop_hhmm = start_hhmm          # not sent
    validate_clock(stop_hhmm)
    weekly = validate_weekly(weekly)
    if start_hhmm == stop_hhmm and not until_soc:
        raise ValueError("start and stop must differ; use Charge now to charge whenever plugged in")
    sh, sm = map(int, start_hhmm.split(":"))
    eh, em = map(int, stop_hhmm.split(":"))
    return _build(sh, sm, eh, em, tz, weekly, END_AT_SOC if until_soc else END_AT_TIME, now,
                  open_now=not until_soc)
