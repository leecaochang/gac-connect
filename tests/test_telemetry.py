import asyncio
from copy import deepcopy

import pytest

from gac_connect.errors import GacError
from gac_connect.models import VehicleStatus, doors_locked
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


@pytest.mark.parametrize("model,expected", [
    (None, None), ("Aion V", None), ("Aion Utopia", None),
    ("Aion UT", True), (" AION UT Premium ", True),
])
def test_ut_lock_sentinel_requires_explicit_model(model, expected):
    raw = {"doorAccessors": [{"door": i, "lock": code} for i, code in enumerate([0, 0, -1, -1], 1)]}
    before = deepcopy(raw)
    status = VehicleStatus.from_results(raw, model=model)
    assert status.locked is expected
    assert doors_locked(status, model) is expected
    assert raw == before
    assert VehicleStatus.from_results(raw).locked is None


@pytest.mark.parametrize("doors,expected", [
    ([{"door": 1, "lock": 1}, {"door": 2, "lock": 0}], False),
    ([{"door": 1, "lock": 0}, {"door": 2, "lock": -1}], None),
    ([{"door": 1, "lock": 0}, {"door": 2, "lock": 0}, {"door": 3, "lock": -1}], None),
    ([{"door": 1, "lock": 0}, {"door": 2, "lock": 0}, {"door": 3, "lock": -1},
      {"door": 3, "lock": -1}], None),
    ([{"door": 1, "lock": True}, {"door": 2, "lock": 0}, {"door": 3, "lock": -1},
      {"door": 4, "lock": -1}], None),
    ([{"door": 1, "lock": "0"}, {"door": 2, "lock": "0"}, {"door": 3, "lock": "-1"},
      {"door": 4, "lock": "-1"}], True),
])
def test_ut_lock_rule_does_not_hide_unlocked_or_malformed_readings(doors, expected):
    status = VehicleStatus.from_results({"doorAccessors": doors}, model="Aion UT")
    assert status.locked is expected
    assert doors_locked(status, "Aion UT") is expected
    assert doors_locked(None, "Aion UT") is None


@pytest.mark.parametrize("value,expected", [
    (-1, None), (-2, None), (128, None), (1638, None), ("-1", None), ("128", None),
    (True, None), (1.5, None), (float("nan"), None), (float("inf"), None), (10**400, None),
    (None, None), (0, 0), (127, 127), ("127", 127), ("64", 64),
])
def test_reported_weekly_mask_has_only_seven_day_bits(value, expected):
    assert VehicleStatus.from_results({"chargingAccessors": [{"weeklyReservation": value}]}).charge_weekly == expected


@pytest.mark.parametrize("items,expected", [
    (None, False), ([], False), ([None], False), ([{}], False), ("bad", False),
    ([None, {"workingMode": 1}], False),
    ([{"workingMode": 3}], True), ([{"workingMode": "unknown"}], True),
])
def test_fridge_requires_a_nonempty_first_report(items, expected):
    raw = {"refrigeratorAccessors": items}
    before = deepcopy(raw)
    status = VehicleStatus.from_results(raw)
    assert status.fridge_fitted is expected
    assert status.raw is raw and raw == before


def test_ut_calendar_reservation_survives_conflicting_timestamp_clock():
    raw = {"timezoneAccessor": {"timezone": "Asia/Bangkok"}, "chargingAccessors": [{
        "chargingMode": 1, "weeklyReservation": 0, "chargingEndType": 0,
        "dailyReservationStartTime": 1791727200000, "dailyReservationStopTime": 1791756000000,
        "startTime": {"year": 2026, "month": 10, "day": 11, "hour": 22, "minute": 0},
        "stopTime": {"year": 2026, "month": 10, "day": 12, "hour": 6, "minute": 0},
    }]}
    before = deepcopy(raw)
    status = VehicleStatus.from_results(raw)
    assert (status.charge_window_start, status.charge_window_stop) == ("22:00", "06:00")
    assert status.charge_time_format == "timestamp"
    assert status.charge_clock_consistent is False
    assert status.charge_end_type == 0
    assert raw == before and status.raw is raw


@pytest.mark.parametrize("zone,start,stop", [
    ("Asia/Bangkok", "21:00", "05:00"), (None, None, None), ("Invalid/Zone", None, None),
])
def test_timestamp_only_reservation_requires_reported_zone(zone, start, stop):
    status = VehicleStatus.from_results({"timezoneAccessor": {"timezone": zone}, "chargingAccessors": [{
        "dailyReservationStartTime": 1791727200000, "dailyReservationStopTime": 1791756000000,
    }]})
    assert (status.charge_window_start, status.charge_window_stop) == (start, stop)
    assert status.charge_clock_consistent is None


def test_malformed_calendar_falls_back_to_legacy_clock():
    status = VehicleStatus.from_results({"chargingAccessors": [{
        "startTime": {"year": 2026, "month": 2, "day": 31, "hour": 22, "minute": 0},
        "stopTime": {"year": True, "month": 10, "day": 12, "hour": 6, "minute": 0},
        "dailyReservationStartTime": 82800000, "dailyReservationStopTime": 18000000,
        "chargingEndType": 1,
    }]})
    assert (status.charge_window_start, status.charge_window_stop) == ("23:00", "05:00")
    assert status.charge_time_format == "time_of_day"
    assert status.charge_end_type == 1


def test_seat_ids_flags_and_duplicates_are_not_inferred_from_array_order():
    status = VehicleStatus.from_results({"seatTemperatureAccessors": [
        {"seat": 2, "enableWind": 1, "windStrength": 3, "temperatureStrength": 0, "temperatureMode": 0},
        {"seat": 1, "enableWind": 0, "windStrength": 0},
        {"seat": 3, "enableWind": -1, "windStrength": -1},
        {"seat": 4, "enableWind": 1}, {"seat": 4, "enableWind": 0},
        {"seat": True, "enableWind": 1}, None,
    ]})
    assert [seat.position for seat in status.seats] == ["1", "2", "3", "4"]
    assert [seat.ventilation_on for seat in status.seats] == [False, True, None, None]
    assert [seat.ventilation_strength for seat in status.seats] == [0, 3, None, None]
    assert status.seats[1].heating_strength == 0


@pytest.mark.parametrize("flag,strength,expected", [
    (1, 0, False), (1, 1, True), (1, 3, True), (0, 3, False),
    (1, None, True), (-1, 1, None), (None, 0, None),
])
def test_seat_ventilation_off_can_keep_the_enable_flag_set(flag, strength, expected):
    status = VehicleStatus.from_results({"seatTemperatureAccessors": [
        {"seat": 1, "enableWind": flag, "windStrength": strength},
    ]})
    assert status.seats[0].ventilation_on is expected


@pytest.mark.parametrize("value,expected", [(0, False), (1, True), (-1, None), (True, None), (1.5, None)])
def test_optional_ac_and_anti_theft_flags_preserve_unknown(value, expected):
    status = VehicleStatus.from_results({
        "airConditionAccessors": [{"enableAutoMode": value, "enableAirCompressor": value}],
        "antiTheftAccessors": [{"enable": value}],
    })
    assert status.ac_auto_on is expected and status.ac_compressor_on is expected
    assert status.anti_theft_on is expected
