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


@pytest.mark.parametrize("speed", [None, 1, 6, 10])
@pytest.mark.parametrize("generic", [False, True])
def test_manual_start_retains_outlets_and_sends_speed_only_when_selected(speed, generic):
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value=ACCEPTED)
    params = {"temperature": 23, "currentDuration": 5}
    if speed is not None:
        params["windStrength"] = speed

    async def run():
        await client.load()
        if generic:
            await client.command("VIN", "aircon-manual-on", **params)
        else:
            await client.climate_manual_on("VIN", temperature=23, minutes=5, fan_speed=speed)

    asyncio.run(run())
    client._iov_call.assert_awaited_once_with(
        f"{PREFIX}/air_condition/open",
        {"identifier": {"vin": "VIN", "grade": 2, "duration": 5},
         "operations": [{"enableAutoMode": 0, "enableAirCompressor": 1,
                         "temperature": 23, "temperatureUnit": 0,
                         **({"windStrength": speed} if speed is not None else {})}]},
        sensitive={"vin": "VIN"}, kind="command",
    )
    http.post.assert_not_called()


@pytest.mark.parametrize("speed", [0, 11, -1, True, "6", 6.5, float("nan"), float("inf")])
@pytest.mark.parametrize("generic", [False, True])
def test_invalid_manual_speed_never_contacts_vehicle(speed, generic):
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock()

    async def run():
        await client.load()
        with pytest.raises(ValueError):
            if generic:
                await client.command("VIN", "aircon-manual-on", windStrength=speed)
            else:
                await client.climate_manual_on("VIN", fan_speed=speed)

    asyncio.run(run())
    client._iov_call.assert_not_awaited()
    http.post.assert_not_called()


def test_rejected_manual_start_never_falls_back_to_auto():
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value={"code": 13101})

    async def run():
        await client.load()
        with pytest.raises(CommandError):
            await client.climate_manual_on("VIN", fan_speed=6)

    asyncio.run(run())
    client._iov_call.assert_awaited_once()
    assert client._iov_call.await_args.args[1]["operations"][0]["enableAutoMode"] == 0
    http.post.assert_not_called()


def test_outlet_candidate_omits_fan_and_recirc_without_changing_start_envelope():
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value=ACCEPTED)

    async def run():
        await client.load()
        await client.climate_manual_on("VIN", temperature=23, minutes=5, blowing_mode=2)

    asyncio.run(run())
    client._iov_call.assert_awaited_once_with(
        f"{PREFIX}/air_condition/open",
        {"identifier": {"vin": "VIN", "grade": 2, "duration": 5},
         "operations": [{"enableAutoMode": 0, "enableAirCompressor": 1,
                         "temperature": 23, "temperatureUnit": 0, "blowingMode": 2}]},
        sensitive={"vin": "VIN"}, kind="command",
    )
    http.post.assert_not_called()


@pytest.mark.parametrize("mode", [0, -1, True, "2", 2.5, float("nan"), float("inf")])
def test_invalid_outlet_candidate_sends_nothing(mode):
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock()
    with pytest.raises(ValueError):
        asyncio.run(client.climate_manual_on("VIN", blowing_mode=mode))
    client._iov_call.assert_not_awaited()
    http.post.assert_not_called()


@pytest.mark.parametrize("cycling", [1, 2])
def test_manual_start_can_select_the_air_source(cycling):
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value=ACCEPTED)

    async def run():
        await client.load()
        await client.climate_manual_on("VIN", temperature=24, minutes=5, cycling_mode=cycling)

    asyncio.run(run())
    body = client._iov_call.await_args.args[1]
    assert body["operations"] == [{"enableAutoMode": 0, "enableAirCompressor": 1, "temperature": 24.0,
                                   "temperatureUnit": 0, "cyclingMode": cycling}]


@pytest.mark.parametrize("value", [0, -1, True, 1.0, "1"])
def test_invalid_air_source_never_sends(value):
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value=ACCEPTED)
    with pytest.raises(ValueError):
        asyncio.run(client.climate_manual_on("VIN", cycling_mode=value))
    client._iov_call.assert_not_called()


@pytest.mark.parametrize("compressor,flag", [(False, 0), (True, 1)])
@pytest.mark.parametrize("generic", [False, True])
def test_manual_start_can_run_the_fan_alone(compressor, flag, generic):
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value=ACCEPTED)

    async def run():
        await client.load()
        if generic:
            await client.command("VIN", "aircon-manual-on", temperature=24, currentDuration=5,
                                 enableAirCompressor=flag, windStrength=3)
        else:
            await client.climate_manual_on("VIN", temperature=24, minutes=5, fan_speed=3, compressor=compressor)

    asyncio.run(run())
    body = client._iov_call.await_args.args[1]
    assert body["operations"] == [{"enableAutoMode": 0, "enableAirCompressor": flag, "temperature": 24.0,
                                   "temperatureUnit": 0, "windStrength": 3}]


@pytest.mark.parametrize("value", [2, -1, None, 0.0, "0", float("nan")])
@pytest.mark.parametrize("generic", [False, True])
def test_invalid_compressor_flag_never_sends(value, generic):
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value=ACCEPTED)

    async def run():
        await client.load()
        if generic:
            await client.command("VIN", "aircon-manual-on", enableAirCompressor=value)
        else:
            await client.climate_manual_on("VIN", compressor=value)

    with pytest.raises(ValueError):
        asyncio.run(run())
    client._iov_call.assert_not_called()
