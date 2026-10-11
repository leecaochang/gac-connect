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
