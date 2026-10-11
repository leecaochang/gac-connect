"""App start/stop envelopes and temperature adjustments that do not request Auto."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from gac_connect.errors import CommandError
from test_failures import Response, make_client

PREFIX = "/iov-vehicle-gateway/v1/platform/vehicle/access_gateway"
ACCEPTED = {"code": 13001, "data": {"sessionId": "climate-request"}}


@pytest.mark.parametrize("temperature,minutes", [(18, 5), (22.3, 30), (32, 60)])
@pytest.mark.parametrize("generic", [False, True])
def test_start_matches_app_envelope_and_places_duration_in_identifier(temperature, minutes, generic):
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value=ACCEPTED)

    async def run():
        await client.load()
        if generic:
            await client.command("VIN", "aircon-on", temperature=temperature, currentDuration=minutes)
        else:
            await client.climate_on("VIN", temperature=temperature, minutes=minutes)

    asyncio.run(run())
    client._iov_call.assert_awaited_once_with(
        f"{PREFIX}/air_condition/open",
        {"identifier": {"vin": "VIN", "grade": 2, "duration": minutes},
         "operations": [{"enableAutoMode": 1, "temperature": round(temperature * 2) / 2,
                         "temperatureUnit": 0}]},
        sensitive={"vin": "VIN"}, kind="command",
    )
    http.post.assert_not_called()


@pytest.mark.parametrize("generic", [False, True])
def test_stop_uses_close_endpoint_and_never_sends_auto_on_or_a_setpoint(generic):
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value=ACCEPTED)

    async def run():
        await client.load()
        if generic:
            await client.command("VIN", "aircon-off")
        else:
            await client.climate_off("VIN")

    asyncio.run(run())
    client._iov_call.assert_awaited_once_with(
        f"{PREFIX}/air_condition/close", {"vin": "VIN", "operations": [{"enableAutoMode": 0}]},
        sensitive={"vin": "VIN"}, kind="command",
    )
    http.post.assert_not_called()


def test_temperature_adjustment_does_not_request_auto_fan_airflow_or_duration():
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value=ACCEPTED)

    async def run():
        await client.load()
        await client.climate_set_temperature("VIN", 22.3)

    asyncio.run(run())
    client._iov_call.assert_awaited_once_with(
        f"{PREFIX}/air_condition/open",
        {"identifier": {"vin": "VIN", "grade": 2},
         "operations": [{"temperature": 22.5, "temperatureUnit": 0}]},
        sensitive={"vin": "VIN"}, kind="command",
    )
    http.post.assert_not_called()


@pytest.mark.parametrize("temperature", [17, 33, True, None, "bad", float("nan"), float("inf")])
def test_invalid_adjustment_never_contacts_the_vehicle(temperature):
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock()
    with pytest.raises(ValueError):
        asyncio.run(client.climate_set_temperature("VIN", temperature))
    client._iov_call.assert_not_awaited()
    http.post.assert_not_called()


def test_rejected_temperature_adjustment_never_falls_back_to_an_automatic_start():
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value={"code": 13101})

    async def run():
        await client.load()
        with pytest.raises(CommandError):
            await client.climate_set_temperature("VIN", 22)

    asyncio.run(run())
    client._iov_call.assert_awaited_once()
    assert client._iov_call.await_args.args[1]["operations"] == [{"temperature": 22, "temperatureUnit": 0}]
    http.post.assert_not_called()
