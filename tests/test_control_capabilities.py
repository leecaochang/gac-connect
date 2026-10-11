"""The app capability query is a read, with no physical command or flag inference."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from gac_connect.errors import ResponseError
from test_failures import Response, make_client

PATH = "/iov-vehicle-gateway/v1/iov/vehicle/access_gateway/get_capability"


@pytest.mark.parametrize("acceptance", [{"success": True}, {"code": "0000"}])
def test_capability_read_preserves_raw_response_and_exact_app_path(acceptance):
    data = {"capabilities": [{"capability": {"remoteCapability": {
        "airConditionCapabilities": [{"blowingModeCapability": 2, "supportBlowingModes": [1, 2, 3]}],
    }}}]}
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value={**acceptance, "data": data})

    async def run():
        await client.load()
        assert await client.get_control_capabilities("VIN") == data

    asyncio.run(run())
    client._iov_call.assert_awaited_once_with(PATH, {"vin": "VIN"}, sensitive={"vin": "VIN"})
    http.post.assert_not_called()


@pytest.mark.parametrize("response", [None, [], {"success": False, "code": 13002},
    {"success": True}, {"success": True, "data": []}, {"data": {"unexpected": True}}])
def test_rejected_or_malformed_capability_read_is_not_a_command_or_success(response):
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value=response)

    async def run():
        await client.load()
        with pytest.raises(ResponseError):
            await client.get_control_capabilities("VIN")

    asyncio.run(run())
    client._iov_call.assert_awaited_once()
    http.post.assert_not_called()


def test_capability_read_can_refresh_and_retry_once():
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(side_effect=[
        {"code": "SDK.GATE.0007"}, {"success": True, "data": {"capabilities": []}},
    ])
    client._refresh = AsyncMock()

    async def run():
        await client.load()
        assert await client.get_control_capabilities("VIN") == {"capabilities": []}

    asyncio.run(run())
    client._refresh.assert_awaited_once_with(stale_token="old")
    assert client._iov_call.await_count == 2
    assert all("kind" not in call.kwargs for call in client._iov_call.await_args_list)
    http.post.assert_not_called()
