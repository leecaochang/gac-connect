from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from gac_connect import GacClient, ResponseError
from gac_connect.client import GacClient as ModuleClient
from gac_connect.errors import GacError


def test_documented_client_is_available_from_package_and_module():
    assert GacClient is ModuleClient
    assert issubclass(ResponseError, GacError)


def test_public_vehicle_behavior_matches_the_client_decoder():
    from gac_connect import VehicleStatus, decode_status, doors_locked, local_mobile

    raw = {"doorAccessors": [{"door": i, "lock": code} for i, code in enumerate([0, 0, -1, -1], 1)],
           "chargingAccessors": [{"chargingMode": "bad"}],
           "drivingOverviewAccessors": [{"remainElectricityPercentage": "0.8"}]}
    status = decode_status(raw, online="1", updated_ms="1791504000000")
    assert status == VehicleStatus.from_results(raw, online="1", updated_ms="1791504000000")
    assert status.soc == 80 and status.charging_mode is None
    assert status.locked is None and doors_locked(status, "Aion UT") is True
    assert decode_status(raw, model="Aion UT").locked is True
    assert local_mobile("+66 61 234 5678", "+66") == "612345678"


def test_public_schedule_helpers_share_validation_and_construction():
    from gac_connect import validate_clock, validate_weekly, window_operation

    assert validate_clock("23:00") == "23:00"
    assert validate_weekly(1.0) == 1
    zone = ZoneInfo("Asia/Bangkok")
    operation = window_operation("23:00", "05:00", zone, now=datetime(2026, 10, 11, 22, tzinfo=zone))
    assert operation["startTime"]["day"] == 11
    assert operation["stopTime"]["day"] == 12


@pytest.mark.parametrize("value", [True, -1, 128, 1.5, "1", float("nan"), float("inf")])
def test_public_weekly_validation_rejects_invalid_masks(value):
    from gac_connect import validate_weekly

    with pytest.raises(ValueError):
        validate_weekly(value)


@pytest.mark.parametrize("value", ["1:00", "24:00", "23:00\n", None, True])
def test_public_clock_validation_rejects_invalid_times(value):
    from gac_connect import validate_clock

    with pytest.raises(ValueError):
        validate_clock(value)
