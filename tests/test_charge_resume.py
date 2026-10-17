"""Charge now and resume keep the owner's reported reservation window."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from gac_connect import vehicle
from gac_connect.errors import CommandError
from gac_connect.models import ChargingMode, VehicleStatus
from test_failures import Response, make_client

RESERVATION = "/iov-vehicle-gateway/v1/platform/vehicle/access_gateway/charging/reservation"
ACCEPTED = {"code": 13001, "data": {"sessionId": "charge"}}
CALENDAR = {"year": 2026, "month": 10, "day": 14, "hour": 23, "minute": 0, "second": 0, "millisecond": 0}
TIME_OF_DAY = {"dailyReservationStartTime": 23 * 3600000, "dailyReservationStopTime": 5 * 3600000,
               "weeklyReservation": 31, "chargingMode": 2, "chargingEndType": 1}
TIMESTAMP = {"dailyReservationStartTime": 1791993600000, "dailyReservationStopTime": 1792015200000,
             "weeklyReservation": 0, "chargingMode": 0, "chargingEndType": 0,
             "startTime": CALENDAR, "stopTime": {**CALENDAR, "day": 15, "hour": 5}}


def _status(charging):
    return {"success": True, "data": {"results": [{"chargingAccessors": [charging]}], "updateTime": 1}}


def _client(charging=None):
    client, http, _ = make_client(Response(body=_status(charging) if charging else None), valid=True)
    return client, http


def _sent(client):
    path, body = client._iov_call.await_args.args
    assert path == RESERVATION
    return body["operations"][0]


@pytest.mark.parametrize("report", [None, TIME_OF_DAY, TIMESTAMP])
@pytest.mark.parametrize("until_soc", [False, True])
def test_charge_now_sends_only_the_mode_and_the_car_keeps_its_window(report, until_soc):
    """An Aion UT refuses free mode with any schedule field (HTTP 400) and accepts the mode alone."""
    client, _ = _client(report)

    async def run():
        await client.load()
        if report:
            await client.get_status("VIN")
        client._iov_call = AsyncMock(return_value=ACCEPTED)
        await client.charge_now("VIN", until_soc=until_soc)

    asyncio.run(run())
    assert _sent(client) == ({"chargingMode": 0, "chargingEndType": 1} if until_soc else {"chargingMode": 0})


def _when(struct):
    from datetime import datetime
    return datetime(*(struct[key] for key in ("year", "month", "day", "hour", "minute")))


@pytest.mark.parametrize("report", [TIME_OF_DAY, TIMESTAMP])
def test_resume_resends_the_reported_window_and_stopping_rule_in_scheduled_mode(report):
    client, _ = _client({**report, "chargingMode": 0})

    async def run():
        await client.load()
        await client.get_status("VIN")
        client._iov_call = AsyncMock(return_value=ACCEPTED)
        await client.resume_schedule("VIN")

    asyncio.run(run())
    op = _sent(client)
    if report["weeklyReservation"] == 0:     # one time: the app's once, dated start and stop
        assert (op["chargingMode"], op["chargingEndType"]) == (1, 0) and "weeklyReservation" not in op
        assert (op["stopTime"]["hour"], op["stopTime"]["minute"]) == (5, 0)
        assert _when(op["stopTime"]) > _when(op["startTime"])
        return
    # repeat (loop) is resumed with times of day, dated for the next occurrence
    assert op["chargingMode"] == 2 and op["chargingEndType"] == report["chargingEndType"]
    assert op["dailyReservationStartTime"] == 23 * 3600000
    assert op["weeklyReservation"] == report["weeklyReservation"]
    assert (op["startTime"]["hour"], op["startTime"]["minute"]) == (23, 0) and op["startTime"]["month"] > 0
    if report["chargingEndType"] == 1:      # stopping at the charge limit sends no stop time
        assert "dailyReservationStopTime" not in op and "stopTime" not in op
    else:          # 05:00 after a 23:00 start is the next day's, sent as 29:00
        assert op["dailyReservationStopTime"] == 29 * 3600000 and op["stopTime"]["hour"] == 5
        assert _when(op["stopTime"]) > _when(op["startTime"])


def test_resume_without_a_reported_window_never_sends():
    client, http = _client()
    client._iov_call = AsyncMock(return_value=ACCEPTED)
    with pytest.raises(CommandError, match="no reported charge window"):
        asyncio.run(client.resume_schedule("VIN"))
    client._iov_call.assert_not_called()
    http.post.assert_not_called()


def test_a_later_report_without_a_window_keeps_the_last_usable_one():
    client, http = _client(TIME_OF_DAY)

    async def run():
        await client.load()
        await client.get_status("VIN")
        http.post.return_value = Response(body=_status({"chargingMode": 0}))
        await client.get_status("VIN")
        client._iov_call = AsyncMock(return_value=ACCEPTED)
        await client.resume_schedule("VIN")

    asyncio.run(run())
    assert _sent(client)["dailyReservationStartTime"] == TIME_OF_DAY["dailyReservationStartTime"]


@pytest.mark.parametrize("charging", [
    None, [], {}, {"dailyReservationStartTime": 1}, {"dailyReservationStartTime": "1", "dailyReservationStopTime": 2},
    {"dailyReservationStartTime": -1, "dailyReservationStopTime": 2},
    {"dailyReservationStartTime": True, "dailyReservationStopTime": 2},
    {"dailyReservationStartTime": 1.5, "dailyReservationStopTime": 2},
])
def test_unusable_reports_give_no_window(charging):
    assert vehicle.reported_window(charging) is None



def test_the_not_set_placeholder_is_no_window():
    # The car's report once a one-time window is gone (the app: "Charging time is not set").
    placeholder = {"chargingMode": 1, "dailyReservationStartTime": 0, "dailyReservationStopTime": 0,
                   "weeklyReservation": 0, "chargingEndType": 0,
                   "startTime": {"year": 2000, "month": 0, "day": 0, "hour": 0, "minute": 0, "second": 0},
                   "stopTime": {"year": 2000, "month": 0, "day": 0, "hour": 0, "minute": 1, "second": 0}}
    assert vehicle.reported_window(placeholder) is None
    status = VehicleStatus.from_results({"chargingAccessors": [placeholder]})
    assert (status.charge_window_start, status.charge_window_stop, status.charge_time_format) == (None, None, None)
    assert status.charging_mode is ChargingMode.SCHEDULED
    midnight = {**placeholder, "dailyReservationStopTime": 3_600_000}     # a real 00:00–01:00 window stays
    assert vehicle.reported_window(midnight) is not None
    assert VehicleStatus.from_results({"chargingAccessors": [midnight]}).charge_window_stop == "01:00"

def test_a_window_kept_in_undated_fields_is_not_the_placeholder():
    # Aion UT 2026-10-17: a one-time 12:00-13:00 window set while charging, reported with times 0.
    report = {"chargingMode": 1, "dailyReservationStartTime": 0, "dailyReservationStopTime": 0,
              "weeklyReservation": 0, "chargingEndType": 0, "chargingStatus": 27,
              "startTime": {"year": 2000, "month": 0, "day": 0, "hour": 12, "minute": 0, "second": 0},
              "stopTime": {"year": 2000, "month": 0, "day": 0, "hour": 13, "minute": 0, "second": 0}}
    status = VehicleStatus.from_results({"chargingAccessors": [report]})
    assert (status.charge_window_start, status.charge_window_stop) == ("12:00", "13:00")
    window = vehicle.reported_window(report)
    assert (window["dailyReservationStartTime"], window["dailyReservationStopTime"]) == (43_200_000, 46_800_000)


def test_reported_window_drops_malformed_parts():
    window = vehicle.reported_window({
        "dailyReservationStartTime": 1.0, "dailyReservationStopTime": 2, "weeklyReservation": 200,
        "chargingEndType": 7, "startTime": {**CALENDAR, "minute": None}, "stopTime": {**CALENDAR, "second": None},
    })
    assert window == {"dailyReservationStartTime": 1, "dailyReservationStopTime": 2, "weeklyReservation": 0,
                      "chargingEndType": 0, "stopTime": {**CALENDAR, "second": 0}}


def test_resume_keeps_the_last_reported_loop_mode_after_free_charging():
    client, http = _client({**TIME_OF_DAY, "chargingMode": 2})

    async def run():
        await client.load()
        await client.get_status("VIN")
        http.post.return_value = Response(body=_status({**TIME_OF_DAY, "chargingMode": 0}))
        await client.get_status("VIN")
        client._iov_call = AsyncMock(return_value=ACCEPTED)
        await client.resume_schedule("VIN")

    asyncio.run(run())
    assert _sent(client)["chargingMode"] == 2


def test_resume_without_a_scheduled_report_uses_loop_mode():
    client, _ = _client({**TIME_OF_DAY, "chargingMode": 0})

    async def run():
        await client.load()
        await client.get_status("VIN")
        client._iov_call = AsyncMock(return_value=ACCEPTED)
        await client.resume_schedule("VIN")

    asyncio.run(run())
    assert _sent(client)["chargingMode"] == 2


# An Aion UT's report of an app-made Sat+Sun 22:08–22:10 repeating window (2026-10-13).
UT_LOOP = {"dailyReservationStartTime": 79680000, "dailyReservationStopTime": 79800000, "weeklyReservation": 96,
           "chargingMode": 2, "chargingEndType": 0, "reservationChargingFlag": 1,
           "startTime": {"month": 0, "hour": 22, "year": 2000, "millisecond": 0, "day": 0, "minute": 8, "second": 0},
           "stopTime": {"month": 0, "hour": 22, "year": 2000, "millisecond": 0, "day": 0, "minute": 10, "second": 0}}


def test_ut_app_window_resumes_in_loop_mode_dated_for_the_next_weekend_day():
    from datetime import datetime
    client, _ = _client(UT_LOOP)

    async def run():
        await client.load()
        await client.get_status("VIN")
        client._iov_call = AsyncMock(return_value=ACCEPTED)
        await client.resume_schedule("VIN")

    asyncio.run(run())
    op = _sent(client)
    assert {key: op[key] for key in ("dailyReservationStartTime", "dailyReservationStopTime", "weeklyReservation",
                                     "chargingMode", "chargingEndType")} == {
        "dailyReservationStartTime": 79680000, "dailyReservationStopTime": 79800000, "weeklyReservation": 96,
        "chargingMode": 2, "chargingEndType": 0}
    start = datetime(*(op["startTime"][key] for key in ("year", "month", "day", "hour", "minute")))
    stop = datetime(*(op["stopTime"][key] for key in ("year", "month", "day", "hour", "minute")))
    assert start.weekday() in (5, 6) and (start.hour, start.minute) == (22, 8)
    assert (stop - start).total_seconds() == 120


def test_undated_reported_fields_are_not_kept():
    window = vehicle.reported_window(UT_LOOP)
    assert "startTime" not in window and "stopTime" not in window
    assert window["chargingMode"] == 2 and window["weeklyReservation"] == 96


# An Aion UT's report of an app-made once window, 22:06–22:12 on 13 Oct 2026 (Singapore gateway).
UT_ONCE = {"dailyReservationStartTime": 1791900360000, "dailyReservationStopTime": 1791900720000,
           "weeklyReservation": 0, "chargingMode": 1, "chargingEndType": 0, "reservationChargingFlag": 1,
           "startTime": {"month": 10, "hour": 22, "year": 2026, "millisecond": 0, "day": 13, "minute": 6, "second": 0},
           "stopTime": {"month": 10, "hour": 22, "year": 2026, "millisecond": 0, "day": 13, "minute": 12, "second": 0}}


def test_resuming_an_app_once_window_rebuilds_it_for_the_next_occurrence():
    client, _ = _client(UT_ONCE)

    async def run():
        await client.load()
        await client.get_status("VIN")
        client._iov_call = AsyncMock(return_value=ACCEPTED)
        await client.charge_now("VIN")
        await client.resume_schedule("VIN")

    asyncio.run(run())
    op = _sent(client)   # a reported once window is resent as the app's once, dated for the next 22:06
    assert (op["chargingMode"], op["chargingEndType"]) == (1, 0) and "weeklyReservation" not in op
    assert (op["stopTime"]["hour"], op["stopTime"]["minute"]) == (22, 12)
    assert _when(op["stopTime"]) > _when(op["startTime"])


# An app-made repeating window across midnight on an Aion UT (23:09–00:10, Fri and Sat).
APP_OVERNIGHT = {"dailyReservationStartTime": 83340000, "dailyReservationStopTime": 87000000,
                 "weeklyReservation": 48, "chargingMode": 2, "chargingEndType": 0,
                 "startTime": {**CALENDAR, "year": 0, "month": 0, "day": 0, "hour": 23, "minute": 9},
                 "stopTime": {**CALENDAR, "year": 0, "month": 0, "day": 0, "hour": 0, "minute": 10}}


def test_a_window_across_midnight_reads_and_resumes_as_the_app_set_it():
    from gac_connect.models import VehicleStatus
    status = VehicleStatus.from_results({"chargingAccessors": [APP_OVERNIGHT]})
    assert (status.charge_window_start, status.charge_window_stop) == ("23:09", "00:10")
    assert status.charge_time_format == "time_of_day"
    client, _ = _client({**APP_OVERNIGHT, "chargingMode": 0})

    async def run():
        await client.load()
        await client.get_status("VIN")
        client._iov_call = AsyncMock(return_value=ACCEPTED)
        await client.resume_schedule("VIN")

    asyncio.run(run())
    op = _sent(client)
    assert (op["dailyReservationStartTime"], op["dailyReservationStopTime"]) == (83340000, 87000000)
    assert op["weeklyReservation"] == 48 and _when(op["stopTime"]) > _when(op["startTime"])


def test_an_ended_charge_restarts_with_a_one_time_window_from_the_next_minute():
    """Aion UT, 2026-10-17: with status 4 (a window ended) or 23 (the connector unlocked) the
    schedule off did not charge again; a one-time window from the next minute did."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Asia/Bangkok")
    operation = vehicle.restart_operation(zone, datetime(2026, 10, 17, 11, 18, 50, tzinfo=zone))
    assert operation == {"chargingMode": 1, "chargingEndType": 1,
                         "startTime": {"year": 2026, "month": 10, "day": 17, "hour": 11, "minute": 20,
                                       "second": 0, "millisecond": 0}}
    assert vehicle.restart_operation(zone, datetime(2026, 10, 17, 23, 59, 10, tzinfo=zone))["startTime"][
        "day"] == 18
    client, _ = _client()

    async def run():
        await client.load()
        client._iov_call = AsyncMock(return_value=ACCEPTED)
        await client.restart_charging("VIN")

    asyncio.run(run())
    sent = _sent(client)
    assert (sent["chargingMode"], sent["chargingEndType"], set(sent)) == (1, 1, {"chargingMode", "chargingEndType",
                                                                                 "startTime"})


PLACEHOLDER = {"dailyReservationStartTime": 0, "dailyReservationStopTime": 0,
               "startTime": {"year": 2000, "month": 0, "day": 0, "hour": 0, "minute": 0},
               "stopTime": {"year": 2000, "month": 0, "day": 0, "hour": 0, "minute": 1}}
ONCE = {"dailyReservationStartTime": 1792207200000, "dailyReservationStopTime": 1792207320000}


@pytest.mark.parametrize("charging,plug,needed", [
    ({"chargingStatus": 4, "chargingMode": 0, **ONCE}, 1, True),     # schedule off after the window ended
    ({"chargingStatus": 4, "chargingMode": 1, **ONCE}, 1, True),
    ({"chargingStatus": 27, "chargingMode": 1, **PLACEHOLDER}, 1, True),   # the app's "not set"
    ({"chargingStatus": 27, "chargingMode": 1, **ONCE}, 1, False),   # waiting for a window ahead
    ({"chargingStatus": 4, "chargingMode": 2, **ONCE}, 1, False),    # a repeating schedule is kept
    ({"chargingStatus": 4, "chargingMode": 0, **ONCE}, 0, False),    # unplugged
    ({"chargingStatus": 2, "chargingMode": 0, **ONCE}, 1, False),
    ({"chargingStatus": 1, "chargingMode": 0, **ONCE}, 1, False),    # the charger's supply is off
    ({"chargingStatus": 7, "chargingMode": 0, **ONCE}, 1, False),
    ({"chargingStatus": 23, "chargingMode": 0, **ONCE}, 1, True),    # the connector unlocked, cable in
    ({"chargingStatus": 0, "chargingMode": 0, **ONCE}, 1, True),
])
def test_restart_is_needed_only_for_an_ended_charge_while_plugged_in(charging, plug, needed):
    from gac_connect import decode_status, restart_needed

    status = decode_status({"chargingAccessors": [charging], "chargerAccessors": [{"plug": plug, "lock": 0}]})
    assert restart_needed(status) is needed
    assert restart_needed(None) is False


def test_a_one_time_window_closing_before_the_next_minute_is_refused():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Asia/Bangkok")
    with pytest.raises(vehicle.WindowClosingError, match="later stop time"):
        vehicle.window_operation("10:00", "11:11", zone, now=datetime(2026, 10, 17, 11, 10, 40, tzinfo=zone))
