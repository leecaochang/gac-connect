"""Seat controls use the request format traced in GAC Android 2.0.32."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from gac_connect.errors import CommandError
from test_failures import Response, make_client


@pytest.mark.parametrize("seat", [1, 2, 3, 4])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("generic", [False, True])
def test_seat_ventilation_matches_app_request_and_omits_temperature_fields(seat, enabled, generic):
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value={"code": 13001, "data": {"sessionId": "seat-request"}})

    async def run():
        await client.load()
        if generic:
            await client.command("VIN", f"seat-{seat}-ventilation-{'on' if enabled else 'off'}")
        else:
            await client.set_seat_ventilation("VIN", seat, enabled)

    asyncio.run(run())
    client._iov_call.assert_awaited_once_with(
        "/iov-vehicle-gateway/v1/platform/vehicle/access_gateway/control_seat_temperature",
        {"identifier": {"vin": "VIN", "grade": 2, "duration": 30},
         "operations": [{"seat": seat, "enableWind": 1, "windStrength": int(enabled)}]},
        sensitive={"vin": "VIN"}, kind="command",
    )
    http.post.assert_not_called()


@pytest.mark.parametrize("seat,enabled", [(0, True), (5, True), (True, True), (1.0, True),
                                         ("1", True), (1, 1), (1, "false"), (1, None)])
def test_invalid_seat_inputs_never_send(seat, enabled):
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock()
    with pytest.raises(ValueError):
        asyncio.run(client.set_seat_ventilation("VIN", seat, enabled))
    client._iov_call.assert_not_awaited()
    http.post.assert_not_called()


def test_rejected_seat_request_is_not_replayed():
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value={"code": 13101})

    async def run():
        await client.load()
        with pytest.raises(CommandError):
            await client.set_seat_ventilation("VIN", 1, True)

    asyncio.run(run())
    client._iov_call.assert_awaited_once()
    http.post.assert_not_called()
