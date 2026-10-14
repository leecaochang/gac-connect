import asyncio
from unittest.mock import AsyncMock

import pytest

from gac_connect.commands import CATALOG
from gac_connect.errors import CommandError, PinRequiredError
from test_failures import Response, make_client


@pytest.mark.parametrize("generic", [False, True])
@pytest.mark.parametrize("response", [{"code": 13001}, {"success": True}, {"success": "false"}, {}])
def test_unlock_uses_normal_command_validation_without_a_pin(generic, response):
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value=response)

    async def run():
        await client.load()
        action = client.command("VIN", "unlock") if generic else client.unlock("VIN")
        if response.get("code") == 13001 or response.get("success") is True:
            assert await action == response
        else:
            with pytest.raises(CommandError):
                await action

    asyncio.run(run())
    client._iov_call.assert_awaited_once_with(
        CATALOG["unlock"].path, {"vin": "VIN"}, sensitive={"vin": "VIN"}, kind="command",
    )
    http.post.assert_not_called()


@pytest.mark.parametrize("name", ["power-on", "power-off"])
def test_other_pin_guards_remain_in_force(name):
    client, http, _ = make_client(Response(), valid=True)
    with pytest.raises(PinRequiredError):
        asyncio.run(client.command("VIN", name))
    http.post.assert_not_called()


def test_the_charging_connector_unlocks_without_a_pin():
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value={"code": 13001})

    async def run():
        await client.load()
        assert await client.command("VIN", "charger-unlock") == {"code": 13001}

    asyncio.run(run())
    client._iov_call.assert_awaited_once_with(
        CATALOG["charger-unlock"].path, {"vin": "VIN"}, sensitive={"vin": "VIN"}, kind="command",
    )
    assert CATALOG["charger-unlock"].path.endswith("/access_gateway/charger/unlock")
