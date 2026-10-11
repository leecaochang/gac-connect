import asyncio
from copy import deepcopy

import pytest

from gac_connect.errors import GacError
from gac_connect.models import VehicleStatus
from test_failures import Response, make_client


@pytest.mark.parametrize("bad", ["unexpected", float("inf"), float("nan"), True, [], {}, 1.5, -1, 10**400])
def test_bad_enum_does_not_discard_other_readings(bad):
    raw = {"chargingAccessors": [{"chargingMode": bad}], "environmentAccessors": [{"temperatureInsideCar": 23}]}
    client, _http, _ = make_client(Response(body={"data": {"results": [raw]}}), valid=True)
    async def run():
        await client.load()
        status = await client.get_status("VIN")
        assert status.charging_mode is None
        assert status.cabin_temp_c == 23
    asyncio.run(run())


@pytest.mark.parametrize("items,locked,opened", [
    ([{"lock": "0", "openMode": "0"}], True, False),
    ([{"lock": 0, "openMode": 0}, {"lock": -1, "openMode": -1}], None, None),
    ([{"lock": -1, "openMode": -1}, {"lock": 1, "openMode": 1}], False, True),
    ([None], None, None), ([], None, None), ({"lock": 0}, None, None),
])
def test_unknown_and_partial_door_reports(items, locked, opened):
    status = VehicleStatus.from_results({"doorAccessors": items})
    assert status.locked is locked
    assert status.door_open is opened


def test_malformed_first_accessor_is_not_replaced_by_later_value():
    status = VehicleStatus.from_results({"drivingOverviewAccessors": [None, {"remainElectricityPercentage": 0.8}]})
    assert status.soc is None


def test_stable_tyre_ids_flags_and_placeholders_preserve_raw():
    raw = {"tyreAccessors": [
        {"tyre": "3", "pressure": 240, "temperature": -1},
        {"tyre": 1, "pressure": -1, "temperature": -1},
        {"tyre": 2, "pressure": 220, "pressureFlag": "0"},
        {"tyre": 4, "pressure": 250}, {"tyre": 4, "pressure": 260}, {"tyre": []}, None,
    ]}
    original = deepcopy(raw)
    status = VehicleStatus.from_results(raw)
    assert [t.position for t in status.tyres] == ["1", "2", "3", "4"]
    assert [t.pressure_kpa for t in status.tyres] == [None, None, 240, None]
    assert status.tyres[0].temperature_c is None
    assert status.tyres[2].temperature_c == -1
    assert raw == original and status.raw is raw


@pytest.mark.parametrize("results", [None, [], [None], {}, "bad"])
def test_bad_status_envelope_fails_clearly(results):
    client, _, _ = make_client(Response(body={"data": {"results": results}}), valid=True)
    async def run():
        await client.load()
        with pytest.raises(GacError, match="report"):
            await client.get_status("VIN")
    asyncio.run(run())
