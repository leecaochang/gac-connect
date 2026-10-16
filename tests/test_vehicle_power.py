"""Vehicle-power calibration and the app's separate shutdown request."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from gac_connect.capabilities import Capabilities, Support
from gac_connect.errors import CommandError
from gac_connect.models import VehicleStatus
from test_failures import Response, make_client


# 1 is the car switched on while stationary (Aion UT, sitting in the parked car).
@pytest.mark.parametrize("mode,expected", [(2, True), (1, True), (0, False), ("2", True), ("1", True), ("0", False)])
@pytest.mark.parametrize("ac_on", [False, True])
def test_power_modes_from_ut_reports_are_independent_of_connectivity_engine_and_ac(mode, expected, ac_on):
    # The supplied on/off reports share online=True, engine.enable=1 and start=-1.
    raw = {"powerAccessors": [{"powerMode": mode}],
           "engineAccessors": [{"enable": 1, "start": -1}],
           "airConditionAccessors": [{"enableAirCompressor": int(ac_on), "windStrength": int(ac_on)}]}
    status = VehicleStatus.from_results(raw, online=True)
    assert status.power_on is expected
    assert status.power_mode == int(mode)
    assert status.online is True and status.ac_on is ac_on
    assert status.raw is raw


@pytest.mark.parametrize("mode,raw_code", [
    (3, 3), (4, 4), (-1, -1), (None, None), (True, None), (False, None),
    (1.5, None), (float("inf"), None), (float("nan"), None), (10**400, None), ("bad", None),
])
def test_unsupported_and_invalid_modes_stay_unknown(mode, raw_code):
    status = VehicleStatus.from_results({"powerAccessors": [{"powerMode": mode}]}, online=True)
    assert status.power_on is None and status.power_mode == raw_code


@pytest.mark.parametrize("items", [None, [], {}, [None], [None, {"powerMode": 2}], [{}]])
def test_missing_or_malformed_power_group_never_means_off(items):
    status = VehicleStatus.from_results({"powerAccessors": items})
    assert status.power_on is None and status.power_mode is None


def test_power_readings_and_shutdown_support_are_separate_evidence():
    caps = Capabilities("Aion UT")
    caps.observe(VehicleStatus.from_results({"powerAccessors": [{"powerMode": 0}]}))
    assert caps.feature("power_on").state is Support.SUPPORTED
    assert caps.feature("vehicle_power_control").state is Support.UNKNOWN
    assert caps.command("vehicle-power-off").state is Support.UNKNOWN
    restored = Capabilities()
    restored.restore(caps.export_state())
    assert restored.feature("power_on").state is Support.SUPPORTED
    assert restored.confirm_command("vehicle-power-off")
    assert restored.feature("vehicle_power_control").state is Support.SUPPORTED
    assert restored.command("power-on").state is Support.UNKNOWN


@pytest.mark.parametrize("response,accepted", [
    ({"code": 13001, "data": {"sessionId": "shutdown"}}, True),
    ({"code": 13101}, False), ({"success": "false"}, False), ({}, False),
])
def test_vehicle_shutdown_matches_the_app_body_and_never_replays_a_rejection(response, accepted):
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value=response)

    async def run():
        await client.load()
        if accepted:
            assert await client.command("VIN", "vehicle-power-off") == response
        else:
            with pytest.raises(CommandError):
                await client.command("VIN", "vehicle-power-off")

    asyncio.run(run())
    client._iov_call.assert_awaited_once_with(
        "/iov-vehicle-gateway/v1/platform/vehicle/access_gateway/power/off", {"vin": "VIN"},
        sensitive={"vin": "VIN"}, kind="command",
    )
    http.post.assert_not_called()


def test_shutdown_rejects_parameters_before_any_request():
    client, http, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock()
    with pytest.raises(CommandError, match="no parameters"):
        asyncio.run(client.command("VIN", "vehicle-power-off", duration=30))
    client._iov_call.assert_not_awaited()
    http.post.assert_not_called()
