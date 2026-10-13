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
def test_overnight_dates_are_ordered_at_every_hour(hour):
    now = datetime(2026, 10, 11, hour, tzinfo=ZONE)
    op = window_operation("23:00", "05:00", ZONE, now=now, once=True)
    start, stop = stamp(op["startTime"]), stamp(op["stopTime"])
    assert now < start < stop
    assert (stop - start).total_seconds() == 6 * 3600
    assert op["dailyReservationStartTime"] == 23 * 3600000


def test_next_start_respects_weekday_mask():
    now = datetime(2026, 10, 11, 2, tzinfo=ZONE)  # Sunday
    op = window_operation("23:00", "05:00", ZONE, weekly=1, now=now, timestamp_times=True)
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


def test_timestamp_reservation_uses_the_same_dates_and_zone_as_calendar_fields():
    now = datetime(2026, 10, 11, 13, tzinfo=ZONE)
    op = window_operation("22:00", "06:00", ZONE, now=now, timestamp_times=True)
    assert datetime.fromtimestamp(op["dailyReservationStartTime"] / 1000, ZONE) == stamp(op["startTime"])
    assert datetime.fromtimestamp(op["dailyReservationStopTime"] / 1000, ZONE) == stamp(op["stopTime"])
    assert op["dailyReservationStopTime"] - op["dailyReservationStartTime"] == 8 * 3600000


def test_client_learns_reservation_format_per_vehicle_without_sending_test_commands():
    import asyncio
    from test_failures import Response, make_client

    raw = {"chargingAccessors": [{
        "dailyReservationStartTime": 1791727200000, "dailyReservationStopTime": 1791756000000,
    }]}
    client, _, _ = make_client(Response(body={"data": {"results": [raw]}}), valid=True)
    client._reservation = AsyncMock()

    async def run():
        await client.load()
        await client.get_status("timestamp-car")
        client._reservation.assert_not_awaited()
        await client.set_charge_window("timestamp-car", "22:00", "06:00")
        op = client._reservation.await_args.args[1]
        assert op["dailyReservationStartTime"] > 86_400_000
        assert op["dailyReservationStopTime"] > op["dailyReservationStartTime"]
        await client.set_charge_window("legacy-car", "22:00", "06:00")
        assert client._reservation.await_args.args[1]["dailyReservationStartTime"] == 22 * 3600000

    asyncio.run(run())


@pytest.mark.parametrize("weekly,days", [(0, 127), (96, 96), (127, 127), (1, 1)])
def test_repeating_window_matches_the_apps_loop_report(weekly, days):
    """An app-made Sat+Sun 22:08–22:10 window is reported exactly like this by an Aion UT."""
    op = window_operation("22:08", "22:10", ZONE, weekly=weekly, now=datetime(2026, 10, 13, 10, tzinfo=ZONE))
    undated = {"year": 2000, "month": 0, "day": 0, "second": 0, "millisecond": 0}
    assert op == {"dailyReservationStartTime": 79680000, "dailyReservationStopTime": 79800000,
                  "weeklyReservation": days, "chargingMode": 2, "chargingEndType": 0,
                  "startTime": {**undated, "hour": 22, "minute": 8}, "stopTime": {**undated, "hour": 22, "minute": 10}}


def test_one_time_window_uses_once_mode_with_dates():
    now = datetime(2026, 10, 13, 23, 30, tzinfo=ZONE)
    op = window_operation("23:00", "05:00", ZONE, now=now, once=True, until_soc=True)
    assert (op["chargingMode"], op["weeklyReservation"], op["chargingEndType"]) == (1, 0, 1)
    assert stamp(op["startTime"]).isoformat() == "2026-10-14T23:00:00+07:00"
    assert op["dailyReservationStartTime"] == 23 * 3600000


@pytest.mark.parametrize("kw", [{"once": True, "weekly": 3}, {"once": "yes"}])
def test_one_time_window_rejects_days_and_non_boolean_flags(kw):
    with pytest.raises(ValueError):
        window_operation("23:00", "05:00", ZONE, **kw)
