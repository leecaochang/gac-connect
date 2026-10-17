from datetime import datetime
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from gac_connect.client import GacClient
from gac_connect.vehicle import window_operation

ZONE = ZoneInfo("Asia/Bangkok")


def stamp(value):
    return datetime(**{k: v for k, v in value.items() if k != "millisecond"}, tzinfo=ZONE)


@pytest.mark.parametrize("hour", range(24))
@pytest.mark.parametrize("weekly", [0, 127])
def test_overnight_windows_are_dated_open_now_or_next_at_every_hour(hour, weekly):
    now = datetime(2026, 10, 11, hour, tzinfo=ZONE)
    op = window_operation("23:00", "05:00", ZONE, now=now, weekly=weekly)
    start, stop = stamp(op["startTime"]), stamp(op["stopTime"])
    assert start < stop and now < stop and (stop.hour, stop.minute) == (5, 0)
    open_now = hour < 5 or hour == 23
    if weekly == 0 and open_now:    # one time, open now: the car never starts a past start
        assert 30 <= (start - now).total_seconds() <= 90
    else:
        assert (start <= now) is open_now      # the window open now, else the next one
        assert (stop - start).total_seconds() == 6 * 3600


def test_next_start_respects_weekday_mask():
    now = datetime(2026, 10, 11, 2, tzinfo=ZONE)  # Sunday
    op = window_operation("23:00", "05:00", ZONE, weekly=1, now=now)
    assert stamp(op["startTime"]).isoformat() == "2026-10-12T23:00:00+07:00"
    assert stamp(op["stopTime"]).isoformat() == "2026-10-13T05:00:00+07:00"


@pytest.mark.parametrize("kw", [
    {"start": "25:00"}, {"stop": "1:00"}, {"stop": "23:00"}, {"start": None},
    *[{"weekly": v} for v in (-1, 128, 1.5, True, "1", None, float("nan"), float("inf"), 10**400)],
    {"until_soc": "false"},
])
def test_invalid_window_never_sends(kw):
    import asyncio
    client = GacClient("AU", Mock())
    client._reservation = AsyncMock()
    with pytest.raises(ValueError):
        asyncio.run(client.set_charge_window("VIN", **{"start": "23:00", "stop": "05:00", **kw}))
    client._reservation.assert_not_called()


@pytest.mark.parametrize("weekly,days,start_day", [(96, 96, 17), (127, 127, 13), (1, 1, 19), (18, 18, 13)])
def test_selected_days_repeat_in_loop_mode_with_times_of_day(weekly, days, start_day):
    """The app's repeat: loop mode, times of day, calendar fields dated for the next selected day."""
    op = window_operation("22:08", "22:10", ZONE, weekly=weekly, now=datetime(2026, 10, 13, 10, tzinfo=ZONE))  # Tue
    dated = {"year": 2026, "month": 10, "day": start_day, "second": 0, "millisecond": 0}
    assert op == {"dailyReservationStartTime": 79680000, "dailyReservationStopTime": 79800000,
                  "weeklyReservation": days, "chargingMode": 2, "chargingEndType": 0,
                  "startTime": {**dated, "hour": 22, "minute": 8}, "stopTime": {**dated, "hour": 22, "minute": 10}}


def test_no_days_is_the_apps_once_with_a_dated_start_and_stop():
    """Aion UT 2026-10-17: this form started and stopped on time. Loop mode without days was
    accepted too, but the car kept only undated hours and never started it."""
    op = window_operation("22:06", "22:12", ZONE, now=datetime(2026, 10, 13, 12, tzinfo=ZONE))
    dated = {"year": 2026, "month": 10, "day": 13, "second": 0, "millisecond": 0}
    assert op == {"chargingMode": 1, "chargingEndType": 0,
                  "startTime": {**dated, "hour": 22, "minute": 6}, "stopTime": {**dated, "hour": 22, "minute": 12}}


@pytest.mark.parametrize("now,start,stop", [
    (datetime(2026, 10, 13, 6, tzinfo=ZONE), "2026-10-13T23:00:00+07:00", "2026-10-14T05:00:00+07:00"),
    # Open now: it starts at the next minute, so the car charges at once.
    (datetime(2026, 10, 13, 23, 30, tzinfo=ZONE), "2026-10-13T23:31:00+07:00", "2026-10-14T05:00:00+07:00"),
    (datetime(2026, 10, 14, 2, 0, 40, tzinfo=ZONE), "2026-10-14T02:02:00+07:00", "2026-10-14T05:00:00+07:00"),
])
def test_one_time_overnight_window_is_dated_open_now_or_next(now, start, stop):
    op = window_operation("23:00", "05:00", ZONE, now=now)
    assert (op["chargingMode"], op["chargingEndType"]) == (1, 0) and "weeklyReservation" not in op
    assert stamp(op["startTime"]).isoformat() == start
    assert stamp(op["stopTime"]).isoformat() == stop
    assert "dailyReservationStartTime" not in op and "dailyReservationStopTime" not in op


def test_a_one_time_window_already_open_today_starts_now_not_tomorrow():
    # Sent at 15:24 for 15:00–03:00: charge from the next minute until 03:00, not from 15:00 tomorrow.
    op = window_operation("15:00", "03:00", ZONE, now=datetime(2026, 10, 14, 15, 24, tzinfo=ZONE))
    assert stamp(op["startTime"]).isoformat() == "2026-10-14T15:25:00+07:00"
    assert stamp(op["stopTime"]).isoformat() == "2026-10-15T03:00:00+07:00"


def test_a_target_level_window_needs_a_start_still_ahead():
    # No stop time is sent, so the car cannot tell the window is open: the next 15:00.
    op = window_operation("15:00", None, ZONE, until_soc=True, now=datetime(2026, 10, 14, 15, 21, tzinfo=ZONE))
    assert stamp(op["startTime"]).isoformat() == "2026-10-15T15:00:00+07:00"


@pytest.mark.parametrize("stop", [None, "05:00"])
def test_one_time_to_the_charge_limit_is_the_apps_once(stop):
    # Aion UT 2026-10-16: this body stopped charging, waited and started at 17:24.
    op = window_operation("17:24", stop, ZONE, until_soc=True, now=datetime(2026, 10, 16, 17, 23, tzinfo=ZONE))
    assert op == {"chargingMode": 1, "chargingEndType": 1, "startTime": {
        "year": 2026, "month": 10, "day": 16, "hour": 17, "minute": 24, "second": 0, "millisecond": 0}}


@pytest.mark.parametrize("stop", ["05:00", "23:00", None])
def test_stopping_at_the_charge_limit_sends_no_stop_time(stop):
    # Verified on an Aion UT: with a stop time the gateway refuses this with 13101.
    op = window_operation("23:00", stop, ZONE, weekly=127, until_soc=True,
                          now=datetime(2026, 10, 13, 12, tzinfo=ZONE))
    assert op == {"dailyReservationStartTime": 23 * 3600000, "weeklyReservation": 127, "chargingMode": 2,
                  "chargingEndType": 1, "startTime": {"year": 2026, "month": 10, "day": 13, "hour": 23,
                                                      "minute": 0, "second": 0, "millisecond": 0}}


@pytest.mark.parametrize("stop", [None, "23:00", "25:00"])
def test_a_window_ending_at_a_time_needs_a_valid_stop_time(stop):
    with pytest.raises(ValueError):
        window_operation("23:00", stop, ZONE)


def test_client_sends_one_time_or_repeating_windows_from_the_selected_days():
    import asyncio
    client = GacClient("TH", Mock())
    client._reservation = AsyncMock()

    async def run():
        await client.set_charge_window("VIN", "22:00", "06:00")
        once = client._reservation.await_args.args[1]
        await client.set_charge_window("VIN", "22:00", "06:00", weekly=96)
        return once, client._reservation.await_args.args[1]

    once, repeat = asyncio.run(run())
    assert once["chargingMode"] == 1 and "weeklyReservation" not in once and once["stopTime"]["hour"] == 6
    assert (repeat["chargingMode"], repeat["weeklyReservation"]) == (2, 96)
