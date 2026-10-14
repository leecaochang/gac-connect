"""Passive equipment discovery from decoded reports and confirmed user actions.

Missing reports never prove absence. Reading a state does not prove that a
remote command works. This module performs no I/O and never probes the vehicle.
Callers may provide explicit equipment flags only from a verified source, such as
``equipment_from_capabilities`` applied to the car's capability list.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Support(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Capability:
    state: Support
    source: str


# Keys describe library readings/equipment, not Home Assistant entity IDs.
READINGS = frozenset({
    "power_on",
    "soc", "range_km", "odometer_km", "cabin_temp_c", "aux_voltage", "pm25",
    "outside_temp_c", "ac_fan_strength", "ac_auto_on", "ac_compressor_on", "anti_theft_on", "battery_precondition_mode",
    "charge_current_a", "charge_voltage_v", "charge_power_kw", "estimated_charge_minutes",
    "charge_window_start", "charge_window_stop", "charge_weekly", "charging_mode", "updated_ms",
    "plugged_in", "charging", "locked", "door_open", "window_open", "hatch_open", "online",
    "charger_locked", "sunroof_open", "steering_heat_on", "lights_on", "ac_on", "ac_target_temp_c",
    "fridge_mode", "fridge_temp_c", "fridge_keep_mode", "fridge_keep_minutes", "latitude", "longitude",
    *(f"tyre_{i}_{kind}" for i in range(1, 5) for kind in ("pressure", "temperature")),
    *(f"seat_{i}_ventilation_on" for i in range(1, 5)),
})
EQUIPMENT_READINGS = {
    "fridge": ("fridge_mode",),
    "steering_heat": ("steering_heat_on",),
    "sunroof": ("sunroof_open",),
    "windows": ("window_open",),
    "tailgate": ("hatch_open",),
    "doors": ("locked", "door_open"),
    "climate": ("ac_on", "ac_target_temp_c"),
    "charge_control": ("charge_window_start", "charge_window_stop", "charge_weekly", "charging_mode",
                       "charging", "plugged_in"),
    "lights": ("lights_on",),
    "location": ("latitude", "longitude"),
    "ventilation": (), "horn": (), "battery_precondition": ("battery_precondition_mode",), "sterilisation": (),
    # A power reading does not establish that shutdown is remotely supported.
    "vehicle_power_control": (),
    # Placeholder off reports do not establish that a seat can be controlled.
    **{f"seat_{i}_ventilation": () for i in range(1, 5)},
}
COMMAND_FEATURES = {
    "vehicle-power-off": "vehicle_power_control",
    "aircon-on": "climate", "aircon-manual-on": "climate",
    "aircon-off": "climate", "aircon-temperature": "climate",
    "fridge-on": "fridge", "fridge-off": "fridge",
    "steering-on": "steering_heat", "steering-off": "steering_heat",
    "sunroof-open": "sunroof", "sunroof-close": "sunroof",
    "window-open": "windows", "window-close": "windows",
    "tailgate-open": "tailgate", "tailgate-close": "tailgate",
    "lock": "doors", "unlock": "doors",
    "charge-now": "charge_control", "charge-resume": "charge_control", "charge-window": "charge_control",
    "charge-schedule": "charge_control",
    "flash-on": "lights", "flash-off": "lights",
    "ventilate-on": "ventilation", "ventilate-off": "ventilation",
    "horn-on": "horn", "horn-off": "horn", "battery-precondition": "battery_precondition",
    "battery-precondition-off": "battery_precondition",
    "antivirus-on": "sterilisation", "antivirus-off": "sterilisation",
    **{f"seat-{i}-ventilation-{direction}": f"seat_{i}_ventilation"
       for i in range(1, 5) for direction in ("on", "off")},
}
FEATURES = READINGS | EQUIPMENT_READINGS.keys()

# The car's capability list (GacClient.get_control_capabilities). Each group has a
# controlCapability switch: 0 means the group is not available, and its other values
# are then defaults. Field levels: 0 none, 1 read, 2 control, 3 read and control.
_READ, _CONTROL = (1, 3), (2, 3)
# feature -> (group, field, levels that grant it). Checked against an Aion UT: absent
# groups (steering, tyres) and switched-off groups (fridge, sunroof, rear seats)
# matched the car, as did its fan, outlet and seat ranges.
_CAPABILITY_FEATURES: dict[str, tuple[str, str, tuple[int, ...]]] = {
    "climate": ("airConditionCapabilities", "temperatureCapability", _CONTROL),
    "ac_target_temp_c": ("airConditionCapabilities", "temperatureCapability", _READ),
    "ac_fan_strength": ("airConditionCapabilities", "windStrengthCapability", _READ),
    "ac_auto_on": ("airConditionCapabilities", "enableAutoModeCapability", _READ),
    "ac_compressor_on": ("airConditionCapabilities", "enableAirCompressorCapability", _READ),
    "sterilisation": ("airConditionCapabilities", "airConditionAntivirusModeCapability", _CONTROL),
    "doors": ("doorCapabilities", "lockCapability", _CONTROL),
    "locked": ("doorCapabilities", "lockCapability", _READ),
    "door_open": ("doorCapabilities", "openModeCapability", _READ),
    "windows": ("windowCapabilities", "openModeCapability", _CONTROL),
    "window_open": ("windowCapabilities", "openModeCapability", _READ),
    # The ventilate command sits with the windows in the app's capability model.
    "ventilation": ("windowCapabilities", "ventilateModeCapability", _CONTROL),
    # Hatch numbers differ from the status report's, so any listed hatch counts.
    "tailgate": ("hatchCapabilities", "openModeCapability", _CONTROL),
    "hatch_open": ("hatchCapabilities", "openModeCapability", _READ),
    "sunroof": ("sunroofCapabilities", "openModeCapability", _CONTROL),
    "sunroof_open": ("sunroofCapabilities", "openModeCapability", _READ),
    "lights": ("lightCapabilities", "openCapability", _CONTROL),
    "lights_on": ("lightCapabilities", "openCapability", _READ),
    "horn": ("hornCapabilities", "hootCapability", _CONTROL),
    "steering_heat": ("steeringCapabilities", "controlCapability", (1, 2, 3)),
    "steering_heat_on": ("steeringCapabilities", "controlCapability", (1, 2, 3)),
    "fridge": ("refrigeratorCapabilities", "refrigeratorCapability", _CONTROL),
    **{reading: ("refrigeratorCapabilities", "refrigeratorCapability", _READ)
       for reading in ("fridge_mode", "fridge_temp_c", "fridge_keep_mode", "fridge_keep_minutes")},
    "battery_precondition": ("batteryPretreatmentCapabilities", "batteryPretreatmentModeCapability", _CONTROL),
    "battery_precondition_mode": ("batteryPretreatmentCapabilities", "batteryPretreatmentModeCapability", _READ),
    "charge_control": ("chargingCapabilities", "chargingModeCapability", _CONTROL),
    "charging_mode": ("chargingCapabilities", "chargingModeCapability", _READ),
    "charging": ("chargingCapabilities", "chargingStatusCapability", _READ),
    "charge_current_a": ("chargingCapabilities", "chargingCurrentCapability", _READ),
    "charge_voltage_v": ("chargingCapabilities", "chargingVoltageCapability", _READ),
    "estimated_charge_minutes": ("chargingCapabilities", "estimatedChargedDurationCapability", _READ),
    "charge_window_start": ("chargingCapabilities", "dailyReservationTimeSettingCapability", _READ),
    "charge_window_stop": ("chargingCapabilities", "dailyReservationTimeSettingCapability", _READ),
    "charge_weekly": ("chargingCapabilities", "weeklyReservationCapability", _READ),
    "plugged_in": ("chargerCapability", "plugCapability", _READ),
    "charger_locked": ("chargerCapability", "lockCapability", _READ),
    "power_on": ("powerCapabilities", "powerCapability", _READ),
    "vehicle_power_control": ("powerCapabilities", "powerCapability", _CONTROL),
    "cabin_temp_c": ("environmentCapabilities", "temperatureInsideCarCapability", _READ),
    "outside_temp_c": ("environmentCapabilities", "temperatureOutsideCarCapability", _READ),
    "pm25": ("environmentCapabilities", "pm25Capability", _READ),
    "soc": ("drivingOverviewCapabilities", "remainElectricityPercentageCapability", _READ),
    "range_km": ("drivingOverviewCapabilities", "remainVoyageCourseCapability", _READ),
    "odometer_km": ("drivingOverviewCapabilities", "totalVoyageCourseCapability", _READ),
    "aux_voltage": ("batteryCapabilities", "voltageCapability", _READ),
    "anti_theft_on": ("antiTheftCapabilities", "enableCapability", _READ),
    "latitude": ("positionCapabilities", "latitudeCapability", _READ),
    "longitude": ("positionCapabilities", "longitudeCapability", _READ),
    "location": ("positionCapabilities", "latitudeCapability", _READ),
}
# Groups listed per seat or tyre, by their ID field.
_SEAT_FEATURES = {f"seat_{i}_ventilation": (i, _CONTROL) for i in range(1, 5)} | {
    f"seat_{i}_ventilation_on": (i, _READ) for i in range(1, 5)}
# Fewer groups than this means a partial answer, which proves nothing about absence.
_MIN_GROUPS = 5


def _level(value: Any) -> int:
    return value if type(value) is int and 0 <= value <= 3 else 0


def _flag(remote: dict, group: str, field: str, levels: tuple[int, ...],
          match: dict[str, Any] | None = None) -> bool | None:
    """True or False from the list; None when an available group does not mention the field."""
    items = [item for item in remote.get(group) or () if isinstance(item, dict)
             and all(item.get(key) == value for key, value in (match or {}).items())]
    available = [item for item in items if _level(item.get("controlCapability")) >= 1]
    if not available:
        return False                      # not listed, or switched off
    if field != "controlCapability" and not any(field in item for item in available):
        return None
    return any(_level(item.get(field)) in levels for item in available)


def equipment_from_capabilities(data: Any) -> dict[str, bool]:
    """What the car's capability list offers remotely, as equipment flags.

    Takes the response of ``GacClient.get_control_capabilities``. A feature whose
    group is missing or switched off is False; one the list does not mention is
    left out. Returns {} for a response too small to rely on.
    """
    records = data.get("capabilities") if isinstance(data, dict) else None
    record = records[0] if isinstance(records, list) and records else data
    capability = record.get("capability", record) if isinstance(record, dict) else None
    remote = capability.get("remoteCapability") if isinstance(capability, dict) else None
    if not isinstance(remote, dict) or sum(isinstance(v, list) for v in remote.values()) < _MIN_GROUPS:
        return {}
    remote = dict(remote)
    if "chargerCapabilities" in remote and "chargerCapability" not in remote:
        remote["chargerCapability"] = remote["chargerCapabilities"]
    flags = {feature: _flag(remote, *spec) for feature, spec in _CAPABILITY_FEATURES.items()}
    for feature, (seat, levels) in _SEAT_FEATURES.items():
        flags[feature] = _flag(remote, "seatTemperatureCapabilities", "enableWindCapability", levels, {"seat": seat})
    for i in range(1, 5):
        for kind, field in (("pressure", "pressureCapability"), ("temperature", "temperatureCapability")):
            flags[f"tyre_{i}_{kind}"] = _flag(remote, "tyreCapabilities", field, _READ, {"tyre": i})
    if flags.get("charge_current_a") is not None and flags.get("charge_voltage_v") is not None:
        flags["charge_power_kw"] = bool(flags["charge_current_a"] and flags["charge_voltage_v"])
    return {feature: value for feature, value in flags.items() if value is not None}


def _known(value: Any) -> bool:
    if isinstance(value, (int, float)):
        try:
            return math.isfinite(value)
        except OverflowError:
            return False
    return isinstance(value, str) and bool(value.strip())


class Capabilities:
    """Evidence for one vehicle. Keep user overrides in the consumer's settings."""

    def __init__(self, model: str | None = None) -> None:
        self.model = (model or "").strip().casefold()
        self._readings: set[str] = set()
        self._commands: set[str] = set()
        self._equipment: dict[str, bool] = {}

    def observe(self, status: Any) -> bool:
        """Remember usable decoded readings, including explicit zero/off values."""
        before = self.export_state()
        for field in READINGS:
            if not field.startswith(("tyre_", "seat_")) and _known(getattr(status, field, None)):
                self._readings.add(field)
        # Match API IDs, never array order. The decoder already rejects flags,
        # invalid/duplicate IDs and paired unreported tyre placeholders.
        for tyre in getattr(status, "tyres", ()):
            if tyre.position not in ("1", "2", "3", "4"):
                continue
            if _known(tyre.pressure_kpa):
                self._readings.add(f"tyre_{tyre.position}_pressure")
            if _known(tyre.temperature_c):
                self._readings.add(f"tyre_{tyre.position}_temperature")
        for seat in getattr(status, "seats", ()):
            if seat.position in ("1", "2", "3", "4") and _known(seat.ventilation_on):
                self._readings.add(f"seat_{seat.position}_ventilation_on")
        return before != self.export_state()

    def observe_equipment(self, flags: dict[str, bool], *, replace: bool = False) -> bool:
        """Accept only explicit booleans for known features from a verified source.

        ``replace`` discards earlier flags first, for a fresh read of the same source.
        """
        before = dict(self._equipment)
        if replace:
            self._equipment.clear()
        self._equipment.update({key: value for key, value in flags.items()
                                if key in FEATURES and isinstance(value, bool)})
        return self._equipment != before

    def confirm_command(self, command: str) -> bool:
        """Record a correlated successful result, never mere request acceptance."""
        if command not in COMMAND_FEATURES or command in self._commands:
            return False
        self._commands.add(command)
        return True

    def _model_absent(self, feature: str) -> bool:
        is_ut = self.model == "aion ut" or self.model.startswith("aion ut ")
        return is_ut and (feature in {"fridge", "steering_heat", "sunroof"} or feature.startswith("tyre_"))

    def feature(self, name: str) -> Capability:
        """Explain equipment/reading support. Positive observations beat presets."""
        if name not in FEATURES:
            return Capability(Support.UNKNOWN, "not_observed")
        if name in self._equipment:
            state = Support.SUPPORTED if self._equipment[name] else Support.UNSUPPORTED
            return Capability(state, "vehicle_equipment")
        if name in self._readings:
            return Capability(Support.SUPPORTED, "observed_telemetry")
        readings = EQUIPMENT_READINGS.get(name, ())
        known = all(field in self._readings for field in readings) if name == "location" else any(
            field in self._readings for field in readings)
        if readings and known:
            return Capability(Support.SUPPORTED, "observed_telemetry")
        if any(COMMAND_FEATURES[command] == name for command in self._commands):
            return Capability(Support.SUPPORTED, "confirmed_command")
        if self._model_absent(name):
            return Capability(Support.UNSUPPORTED, "model_default")
        return Capability(Support.UNKNOWN, "not_observed")

    def command(self, name: str) -> Capability:
        """Opening and closing, and on and off, need separate confirmation."""
        if name == "charge-pause":
            return Capability(Support.UNSUPPORTED, "no_continuous_pause")
        feature = COMMAND_FEATURES.get(name)
        if feature is None:
            return Capability(Support.UNKNOWN, "not_observed")
        equipment = self.feature(feature)
        if equipment.source == "vehicle_equipment" and equipment.state is Support.UNSUPPORTED:
            return equipment
        if name in self._commands:
            return Capability(Support.SUPPORTED, "confirmed_command")
        if equipment.state is Support.UNSUPPORTED:
            return equipment
        return Capability(Support.UNKNOWN, "not_confirmed")

    def export_state(self) -> dict:
        """Store evidence names only, without telemetry, VINs or credentials."""
        return {"version": 1, "readings": sorted(self._readings), "commands": sorted(self._commands),
                "equipment": dict(self._equipment)}

    def restore(self, state: Any) -> None:
        """Ignore malformed/unknown cache entries; missing data stays unknown."""
        if not isinstance(state, dict) or state.get("version") != 1:
            return
        for key, allowed, target in (("readings", READINGS, self._readings),
                                     ("commands", COMMAND_FEATURES, self._commands)):
            values = state.get(key)
            if isinstance(values, list):
                target.update(value for value in values if isinstance(value, str) and value in allowed)
        equipment = state.get("equipment")
        if isinstance(equipment, dict):
            self.observe_equipment(equipment)

    def diagnostics(self) -> dict:
        return {
            "features": {name: {"state": self.feature(name).state.value, "source": self.feature(name).source}
                         for name in sorted(FEATURES)},
            "commands": {name: {"state": self.command(name).state.value, "source": self.command(name).source}
                         for name in (*COMMAND_FEATURES, "charge-pause")},
        }
