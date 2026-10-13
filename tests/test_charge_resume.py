"""Charge now and resume keep the owner's reported reservation window."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from gac_connect import vehicle
from gac_connect.errors import CommandError
from test_failures import Response, make_client

RESERVATION = "/iov-vehicle-gateway/v1/platform/vehicle/access_gateway/charging/reservation"
ACCEPTED = {"code": 13001, "data": {"sessionId": "charge"}}
CALENDAR = {"year": 2026, "month": 10, "day": 14, "hour": 23, "minute": 0, "second": 0, "millisecond": 0}
TIME_OF_DAY = {"dailyReservationStartTime": 23 * 3600000, "dailyReservationStopTime": 5 * 3600000,
               "weeklyReservation": 31, "chargingMode": 1, "chargingEndType": 1}
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


@pytest.mark.parametrize("report", [TIME_OF_DAY, TIMESTAMP])
@pytest.mark.parametrize("until_soc", [False, True])
def test_charge_now_resends_the_reported_window_in_free_mode(report, until_soc):
    client, _ = _client(report)

    async def run():
        await client.load()
        await client.get_status("VIN")
        client._iov_call = AsyncMock(return_value=ACCEPTED)
        await client.charge_now("VIN", until_soc=until_soc)

    asyncio.run(run())
    expected = {key: report[key] for key in ("dailyReservationStartTime", "dailyReservationStopTime",
                                             "weeklyReservation", "startTime", "stopTime") if key in report}
    assert _sent(client) == {**expected, "chargingMode": 0, "chargingEndType": int(until_soc)}


def test_charge_now_before_any_report_keeps_the_legacy_request():
    client, _ = _client()
    client._iov_call = AsyncMock(return_value=ACCEPTED)
    asyncio.run(client.charge_now("VIN"))
    assert _sent(client) == vehicle.charge_now_operation()


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
    assert op["chargingMode"] == 1 and op["chargingEndType"] == report["chargingEndType"]
    assert op["dailyReservationStartTime"] == report["dailyReservationStartTime"]
    assert op.get("startTime") == report.get("startTime")


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


def test_resume_without_a_scheduled_report_uses_once_like_set_charge_window():
    client, _ = _client({**TIME_OF_DAY, "chargingMode": 0})

    async def run():
        await client.load()
        await client.get_status("VIN")
        client._iov_call = AsyncMock(return_value=ACCEPTED)
        await client.resume_schedule("VIN")

    asyncio.run(run())
    assert _sent(client)["chargingMode"] == 1
