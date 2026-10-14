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
def test_overnight_dates_are_ordered_at_every_hour(hour, weekly):
    now = datetime(2026, 10, 11, hour, tzinfo=ZONE)
    op = window_operation("23:00", "05:00", ZONE, now=now, weekly=weekly)
    start, stop = stamp(op["startTime"]), stamp(op["stopTime"])
    assert now < start < stop
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


def test_no_days_is_a_one_time_window_sent_in_loop_mode():
    """No days is the app's Once; mode 1 itself is refused (13101), loop without days is accepted."""
    op = window_operation("22:06", "22:12", ZONE, now=datetime(2026, 10, 13, 12, tzinfo=ZONE))
    dated = {"year": 2026, "month": 10, "day": 13, "second": 0, "millisecond": 0}
    assert op == {"dailyReservationStartTime": 79560000, "dailyReservationStopTime": 79920000,
                  "weeklyReservation": 0, "chargingMode": 2, "chargingEndType": 0,
                  "startTime": {**dated, "hour": 22, "minute": 6}, "stopTime": {**dated, "hour": 22, "minute": 12}}


def test_one_time_overnight_window_is_dated_for_the_next_start():
    op = window_operation("23:00", "05:00", ZONE, now=datetime(2026, 10, 13, 23, 30, tzinfo=ZONE))
    assert (op["chargingMode"], op["weeklyReservation"], op["chargingEndType"]) == (2, 0, 0)
    assert stamp(op["startTime"]).isoformat() == "2026-10-14T23:00:00+07:00"
    assert stamp(op["stopTime"]).isoformat() == "2026-10-15T05:00:00+07:00"
    # A stop after midnight is sent as its time of day plus 24 h, as the app does.
    assert (op["dailyReservationStartTime"], op["dailyReservationStopTime"]) == (23 * 3600000, 29 * 3600000)


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
    assert (once["chargingMode"], once["weeklyReservation"], once["dailyReservationStartTime"]) == (2, 0, 79200000)
    assert (repeat["chargingMode"], repeat["weeklyReservation"]) == (2, 96)
