import asyncio
from copy import deepcopy

import pytest

from gac_connect.errors import GacError
from gac_connect.models import ChargingMode, VehicleStatus, doors_locked
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
    ([{"lock": "0", "openMode": "0"}], None, False),             # one unnumbered door: not the whole car
    ([{"door": n, "lock": 0} for n in (1, 2, 3, 4)], True, None),
    ([{"door": n, "lock": 0} for n in (1, 2)], None, None),       # doors missing
    ([{"door": 1, "lock": 0}] * 4, None, None),                   # one door repeated
    ([{"door": n, "lock": 0} for n in (1, 2, 3, "4")], None, None),
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


def test_ut_once_calendar_and_timestamps_agree_on_the_service_clock():
    """An app-made once window: Unix-ms times encode the calendar clock in UTC+8, not Asia/Bangkok."""
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
    assert status.charge_clock_consistent is True
    assert status.charge_end_type == 0
    assert raw == before and status.raw is raw


@pytest.mark.parametrize("zone", ["Asia/Bangkok", None, "Invalid/Zone"])
def test_timestamp_only_reservation_reads_the_service_clock(zone):
    status = VehicleStatus.from_results({"timezoneAccessor": {"timezone": zone}, "chargingAccessors": [{
        "dailyReservationStartTime": 1791727200000, "dailyReservationStopTime": 1791756000000,
    }]})
    assert (status.charge_window_start, status.charge_window_stop) == ("22:00", "06:00")
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


@pytest.mark.parametrize('code,expected', [(0, False), (1, True), (2, None), (-1, None), (None, None)])
def test_raw_seat_enable_code_is_retained_without_guessing_smart_mode(code, expected):
    status = VehicleStatus.from_results({'seatTemperatureAccessors': [{'seat': 1, 'enableWind': code, 'windStrength': 1}]})
    assert status.seats[0].ventilation_enable_code == code
    assert status.seats[0].ventilation_on is expected


@pytest.mark.parametrize("current,expected", [
    (1638.0, None), (1638.3, None), (1638.4, None), (6553.5, None), (1637.9, 1637.9), (0, 0), (16.5, 16.5), (-1, None),
])
def test_saturated_charge_current_is_unknown_and_never_becomes_power(current, expected):
    status = VehicleStatus.from_results({"chargingAccessors": [{"chargingCurrent": current, "chargingVoltage": 400}]})
    assert status.charge_current_a == expected
    assert status.charge_power_kw == (round(400 * expected / 1000, 2) if expected is not None else None)


@pytest.mark.parametrize("value,expected", [(0, None), (0.0, None), ("0", None), (-5, None), (1, 1), (3205.5, 3205.5)])
def test_zero_odometer_is_a_placeholder(value, expected):
    status = VehicleStatus.from_results({"drivingOverviewAccessors": [{"totalVoyageCourse": value}]})
    assert status.odometer_km == expected


@pytest.mark.parametrize("value,expected", [(0, ChargingMode.FREE), (1, ChargingMode.SCHEDULED), (2, ChargingMode.LOOP),
                                            (3, None), (-1, None), (True, None)])
def test_app_charging_modes_decode_immediately_once_and_loop(value, expected):
    status = VehicleStatus.from_results({"chargingAccessors": [{"chargingMode": value}]})
    assert status.charging_mode is expected
    if expected is not None:
        assert status.charging_mode.scheduled is (value in (1, 2))


@pytest.mark.parametrize("flag,valid", [(0, False), (False, False), (0.0, False), (1, True), (True, True), (None, True),
                                        (-1, True), ("0", True)])
def test_explicitly_invalid_readings_are_unknown(flag, valid):
    status = VehicleStatus.from_results({
        "chargingAccessors": [{"chargingCurrent": 16, "chargingCurrentFlag": flag,
                               "estimatedChargedDuration": 90, "estimatedChargedDurationFlag": flag}],
        "drivingOverviewAccessors": [{"remainVoyageCourse": 300, "remainVoyageCourseFlag": flag}],
        "environmentAccessors": [{"temperatureInsideCar": 31, "temperatureInsideCarFlag": flag}],
        "hatchAccessors": [{"openMode": 1, "openModeFlag": flag}],
    })
    readings = (status.charge_current_a, status.estimated_charge_minutes, status.range_km, status.cabin_temp_c,
                status.hatch_open)
    assert readings == ((16, 90, 300, 31, True) if valid else (None,) * 5)


def test_missing_validity_flags_keep_readings():
    status = VehicleStatus.from_results({"chargingAccessors": [{"chargingCurrent": 16}],
                                         "hatchAccessors": [{"openMode": 0}, {"openMode": 0}]})
    assert status.charge_current_a == 16 and status.hatch_open is False


@pytest.mark.parametrize("mode,on", [(0, False), (1, True), (2, True), (3, True), (-1, None), (4, None), (None, None)])
def test_battery_preconditioning_mode_is_decoded(mode, on):
    status = VehicleStatus.from_results({"batteryPretreatmentAccessors": [
        {"batteryPretreatmentMode": mode, "batteryPretreatmentStatus": -1}]})
    assert status.battery_precondition_on is on
    assert status.battery_precondition_mode == (mode if on is not None else None)
    assert status.battery_precondition_status is None


def _lights(open_code):
    return {"lightAccessors": [{"light": 1, "open": -1}, {"light": 2, "open": open_code}, {"light": 3, "open": -1}]}


@pytest.mark.parametrize("open_code,on", [(1, True), (-1, False), (0, False), (None, None), ("x", None)])
def test_headlights_read_light_2_and_minus_one_is_off(open_code, on):
    # Aion UT: light 2 is 1 at night and -1 in daylight; it is never reported as 0 there.
    assert VehicleStatus.from_results(_lights(open_code)).headlights_on is on
    assert VehicleStatus.from_results({"lightAccessors": [{"light": 1, "open": 1}]}).headlights_on is None


def test_headlights_are_established_only_once_seen_on():
    from gac_connect.capabilities import Capabilities, Support
    caps = Capabilities("Aion UT")
    caps.observe(VehicleStatus.from_results(_lights(-1)))
    assert caps.feature("headlights_on").state is not Support.SUPPORTED
    caps.observe(VehicleStatus.from_results(_lights(1)))
    assert caps.feature("headlights_on").state is Support.SUPPORTED


def _lamps(*on):
    # Aion UT 2026-10-16: lights 1-8 and 10 are reported; off ones as -1.
    return {"lightAccessors": [{"light": n, "open": 1 if n in on else -1} for n in (1, 2, 3, 4, 5, 6, 7, 8, 10)]}


@pytest.mark.parametrize("on,expected", [
    ((2, 3), {"headlights_on": True, "high_beam_on": True}),     # high beams (with the dipped beams)
    ((2, 5), {"headlights_on": True, "rear_fog_on": True}),       # rear fog light
    ((6,), {"turn_signal_on": True}),                             # either indicator, or the hazard lights
    ((7,), {"brake_lights_on": True}),                            # brake pedal pressed
    ((), {}),
])
def test_each_reported_light_reads_on_by_its_number(on, expected):
    status = VehicleStatus.from_results(_lamps(*on))
    for name in ("headlights_on", "high_beam_on", "rear_fog_on", "turn_signal_on", "brake_lights_on"):
        assert getattr(status, name) is expected.get(name, False)
    assert VehicleStatus.from_results({}).high_beam_on is None


def test_lights_are_established_only_once_seen_on():
    from gac_connect.capabilities import Capabilities, Support
    caps = Capabilities("Aion UT")
    caps.observe(VehicleStatus.from_results(_lamps()))
    assert caps.feature("rear_fog_on").state is not Support.SUPPORTED
    caps.observe(VehicleStatus.from_results(_lamps(2, 5)))
    assert caps.feature("rear_fog_on").state is Support.SUPPORTED
    assert caps.feature("turn_signal_on").state is not Support.SUPPORTED


@pytest.mark.parametrize("number,position", [(1, "front_left"), (2, "front_right"), (3, "rear_left"),
                                             (4, "rear_right")])
def test_each_door_and_window_reads_by_its_number(number, position):
    # Aion UT (right-hand drive) 2026-10-16: the front left window is 1, the driver's
    # (front right) door and window are 2, rear left 3, rear right 4.
    report = {kind: [{kind[:-9]: n, "openMode": int(n == number)} for n in (1, 2, 3, 4)]
              for kind in ("doorAccessors", "windowAccessors")}
    status = VehicleStatus.from_results(report)
    for other in ("front_left", "front_right", "rear_left", "rear_right"):
        assert getattr(status, f"{other}_door_open") is (other == position)
        assert getattr(status, f"{other}_window_open") is (other == position)
    assert status.door_open is True and status.window_open is True
    missing = VehicleStatus.from_results({"windowAccessors": [{"window": 1, "openMode": 0}]})
    assert missing.front_left_window_open is False and missing.rear_right_window_open is None
    assert VehicleStatus.from_results({"windowAccessors": [{"window": 1, "openMode": -1}]}).front_left_window_open is None


def test_battery_temperature_follows_the_battery_report():
    status = VehicleStatus.from_results({"batteryAccessors": [{"battery": 1, "temperature": 30.5, "voltage": 12.8}]})
    assert (status.battery_temp_c, status.aux_voltage) == (30.5, 12.8)


@pytest.mark.parametrize("mode,on", [(1, True), (0, False), (3, False), (4, False), (2, None), (-1, None),
                                     (None, None)])
def test_sterilisation_follows_the_antivirus_mode(mode, on):
    # Aion UT, app-started sterilisation: mode 1 with the A/C running hot; 0 once stopped;
    # 3 after a 30-minute run from HA had finished (2026-10-17).
    air = {"airConditionAntivirusMode": mode} if mode is not None else {}
    assert VehicleStatus.from_results({"airConditionAccessors": [air]}).sterilisation_on is on



@pytest.mark.parametrize("code,charging", [(0, False), (1, False), (2, True), (4, False), (7, False), (23, False),
                                           (27, False), (5, None)])
def test_charging_status_codes(code, charging):
    # Aion UT 2026-10-16: 1 plugged in with the charger switched off, 2 charging at 31 A / 225 V,
    # 27 waiting for a scheduled start.
    status = VehicleStatus.from_results({"chargingAccessors": [{"chargingStatus": code}],
                                         "chargerAccessors": [{"plug": 1, "lock": 0}]})
    assert status.charging is charging


@pytest.mark.parametrize("overview,expected", [
    ({"averageElectricityConsumption": 11.8, "electricityConsumptionUnit": 0}, 11.8),
    ({"averageElectricityConsumption": 11.8}, 11.8),
    ({"averageElectricityConsumption": 11.8, "electricityConsumptionUnit": 1}, None),   # unit not decoded
    ({"averageElectricityConsumption": -1, "electricityConsumptionUnit": 0}, None),
])
def test_trip_consumption_reads_the_drive_average_in_kwh_per_100_km(overview, expected):
    status = VehicleStatus.from_results({"drivingOverviewAccessors": [overview]})
    assert status.trip_consumption_kwh_100km == expected
