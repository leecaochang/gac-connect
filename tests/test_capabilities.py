import json

import pytest

from gac_connect.capabilities import Capabilities, Support
from gac_connect.models import VehicleStatus


def test_empty_reports_do_not_prove_unsupported_and_off_values_are_evidence():
    caps = Capabilities()
    assert caps.feature("sunroof").state is Support.UNKNOWN
    caps.observe(VehicleStatus.from_results({"sunroofAccessors": [{"openMode": 0}],
                                            "steeringAccessors": [{"steering": 0}]}))
    assert caps.feature("sunroof").state is Support.SUPPORTED
    assert caps.feature("steering_heat").state is Support.SUPPORTED
    assert caps.command("sunroof-open").state is Support.UNKNOWN
    assert not caps.observe(VehicleStatus.from_results({}))
    assert caps.feature("sunroof").state is Support.SUPPORTED


def test_tyre_readings_are_independent_and_placeholders_are_not_equipment():
    caps = Capabilities()
    caps.observe(VehicleStatus.from_results({"tyreAccessors": [
        {"tyre": 1, "pressure": -1, "temperature": -1, "pressureFlag": 1, "temperatureFlag": 1},
        {"tyre": 3, "pressure": 240, "temperatureFlag": 0, "temperature": 22},
    ]}))
    assert caps.feature("tyre_1_pressure").state is Support.UNKNOWN
    assert caps.feature("tyre_1_temperature").state is Support.UNKNOWN
    assert caps.feature("tyre_3_pressure").state is Support.SUPPORTED
    assert caps.feature("tyre_3_temperature").state is Support.UNKNOWN


def test_fridge_presence_requires_recognized_readings_not_a_dummy_accessor():
    caps = Capabilities()
    caps.observe(VehicleStatus.from_results({"refrigeratorAccessors": [{"workingMode": -1}]}))
    assert caps.feature("fridge").state is Support.UNKNOWN
    caps.observe(VehicleStatus.from_results({"refrigeratorAccessors": [{"workingMode": 3}]}))
    assert caps.feature("fridge").state is Support.SUPPORTED
    assert caps.command("fridge-on").state is Support.UNKNOWN


@pytest.mark.parametrize("mode", [0, 1])
def test_charging_mode_alone_reveals_the_charging_equipment(mode):
    caps = Capabilities()
    caps.observe(VehicleStatus.from_results({"chargingAccessors": [{"chargingMode": mode}]}))
    assert caps.feature("charge_control").state is Support.SUPPORTED
    assert caps.command("charge-now").state is Support.UNKNOWN
    assert caps.command("charge-pause").state is Support.UNSUPPORTED


def test_model_defaults_can_be_corrected_by_positive_variant_evidence():
    caps = Capabilities("Aion UT")
    assert caps.feature("tyre_1_pressure").state is Support.UNSUPPORTED
    assert caps.feature("sunroof").state is Support.UNSUPPORTED
    caps.observe(VehicleStatus(sunroof_open=False))
    assert caps.feature("sunroof").state is Support.SUPPORTED
    assert caps.feature("sunroof").source == "observed_telemetry"
    assert Capabilities("Aion Utopia").feature("sunroof").state is Support.UNKNOWN


def test_commands_are_direction_specific_and_do_not_claim_sensor_support():
    caps = Capabilities()
    assert caps.confirm_command("sunroof-close")
    assert not caps.confirm_command("sunroof-close")
    assert caps.command("sunroof-close").state is Support.SUPPORTED
    assert caps.command("sunroof-open").state is Support.UNKNOWN
    assert caps.feature("sunroof").state is Support.SUPPORTED
    assert caps.feature("sunroof_open").state is Support.UNKNOWN
    assert not caps.confirm_command("invented-command")
    assert caps.command("charge-pause").state is Support.UNSUPPORTED


def test_explicit_equipment_flags_are_strict_and_override_inference():
    caps = Capabilities()
    caps.observe(VehicleStatus(sunroof_open=False))
    caps.observe_equipment({"sunroof": False, "fridge": "true", "secret": True})
    assert caps.feature("sunroof").state is Support.UNSUPPORTED
    assert caps.command("sunroof-open").state is Support.UNSUPPORTED
    assert caps.feature("fridge").state is Support.UNKNOWN
    assert "secret" not in json.dumps(caps.diagnostics())


def test_cache_retains_evidence_without_vehicle_data():
    caps = Capabilities()
    caps.observe(VehicleStatus(soc=65, raw={"vin": "PRIVATE-VIN"}))
    caps.confirm_command("lock")
    state = json.loads(json.dumps(caps.export_state()))
    assert "PRIVATE" not in json.dumps(state)
    restored = Capabilities()
    restored.restore(state)
    restored.observe(VehicleStatus())
    assert restored.feature("soc").state is Support.SUPPORTED
    assert restored.command("lock").state is Support.SUPPORTED


@pytest.mark.parametrize("state", [None, [], {}, {"version": 2}, {"version": 1, "readings": "soc"},
                                  {"version": 1, "readings": [None, {}, "secret"], "commands": [True]}])
def test_invalid_cache_never_creates_capabilities(state):
    caps = Capabilities()
    caps.restore(state)
    assert caps.feature("soc").state is Support.UNKNOWN
    assert caps.command("lock").state is Support.UNKNOWN


def test_seat_and_anti_theft_status_do_not_invent_remote_controls():
    caps = Capabilities("Aion UT")
    caps.observe(VehicleStatus.from_results({"seatTemperatureAccessors": [
        {"seat": 1, "enableWind": 0}, {"seat": 2, "enableWind": -1},
    ], "antiTheftAccessors": [{"enable": 1}]}))
    assert caps.feature("seat_1_ventilation_on").state is Support.SUPPORTED
    assert caps.feature("seat_2_ventilation_on").state is Support.UNKNOWN
    assert caps.feature("anti_theft_on").state is Support.SUPPORTED
    assert caps.feature("seat_1_ventilation").state is Support.UNKNOWN
    assert caps.command("seat-1-ventilation-on").state is Support.UNKNOWN
    caps.confirm_command("seat-1-ventilation-on")
    assert caps.feature("seat_1_ventilation").source == "confirmed_command"
    assert caps.command("seat-1-ventilation-on").state is Support.SUPPORTED
    assert caps.command("seat-1-ventilation-off").state is Support.UNKNOWN
    assert caps.feature("seat_2_ventilation").state is Support.UNKNOWN



def _ut_capabilities():
    import json
    from pathlib import Path
    return json.loads((Path(__file__).parent / "fixtures" / "ut_capabilities.json").read_text())


def test_an_aion_ut_capability_list_matches_the_car():
    from gac_connect import equipment_from_capabilities
    flags = equipment_from_capabilities(_ut_capabilities())
    offered = {"climate", "doors", "windows", "ventilation", "tailgate", "lights", "horn", "battery_precondition",
               "charge_control", "vehicle_power_control", "sterilisation", "seat_1_ventilation", "seat_2_ventilation",
               "soc", "range_km", "odometer_km", "cabin_temp_c", "outside_temp_c", "aux_voltage", "plugged_in",
               "charger_locked", "charging", "charge_current_a", "charge_voltage_v", "charge_power_kw",
               "estimated_charge_minutes", "location", "power_on", "anti_theft_on", "locked", "door_open",
               "window_open", "hatch_open", "lights_on", "ac_fan_strength", "ac_compressor_on", "ac_auto_on",
               "seat_1_ventilation_on", "seat_2_ventilation_on"}
    absent = {"sunroof", "sunroof_open", "steering_heat", "steering_heat_on", "fridge", "fridge_mode", "pm25",
              "seat_3_ventilation", "seat_4_ventilation", "seat_3_ventilation_on", "seat_4_ventilation_on",
              *(f"tyre_{i}_{kind}" for i in range(1, 5) for kind in ("pressure", "temperature"))}
    assert {feature for feature in offered if flags.get(feature) is not True} == set()
    assert {feature for feature in absent if flags.get(feature) is not False} == set()


def test_capability_flags_win_over_placeholder_readings_and_can_be_replaced():
    from gac_connect import Capabilities, Support, equipment_from_capabilities
    from gac_connect.models import VehicleStatus
    caps = Capabilities("Aion UT")
    caps.observe(VehicleStatus(sunroof_open=False, pm25=0.0))      # placeholders the UT sends
    assert caps.feature("sunroof").state is caps.feature("pm25").state is Support.SUPPORTED
    caps.observe_equipment(equipment_from_capabilities(_ut_capabilities()), replace=True)
    for feature in ("sunroof", "pm25"):
        assert (caps.feature(feature).state, caps.feature(feature).source) == (Support.UNSUPPORTED, "vehicle_equipment")
    assert caps.feature("horn").state is Support.SUPPORTED          # a control with no reading
    caps.observe_equipment({"horn": False}, replace=True)
    assert caps.feature("sunroof").state is Support.SUPPORTED       # back to the reading once replaced
    assert caps.feature("horn").state is Support.UNSUPPORTED


def test_a_partial_or_unreadable_list_proves_nothing():
    from gac_connect import equipment_from_capabilities
    for data in (None, {}, {"capabilities": []}, {"capabilities": [{"capability": {"remoteCapability": {
            "hornCapabilities": [{"controlCapability": 1, "hootCapability": 3}]}}}]}):
        assert equipment_from_capabilities(data) == {}


def test_an_available_group_that_omits_a_field_leaves_it_to_the_reports():
    from gac_connect import equipment_from_capabilities
    data = _ut_capabilities()
    window = data["capabilities"][0]["capability"]["remoteCapability"]["windowCapabilities"][0]
    del window["ventilateModeCapability"]
    flags = equipment_from_capabilities(data)
    assert "ventilation" not in flags and flags["windows"] is True
