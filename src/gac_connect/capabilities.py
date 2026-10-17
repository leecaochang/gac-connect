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

from .commands import (
    CLIMATE_MINUTES_RANGE,
    CLIMATE_TEMP_RANGE,
    FAN_SPEEDS,
    FRIDGE_MODES,
    FRIDGE_TEMP_RANGE,
    SEAT_VENTILATION_LEVELS,
)


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
    "plugged_in", "charging", "locked", "door_open", "window_open", "hatch_open", "hood_open", "online",
    "brake_fluid_low", "battery_temp_c", "trip_consumption_kwh_100km", "headlights_on", "sterilisation_on",
    "high_beam_on", "rear_fog_on", "turn_signal_on", "brake_lights_on",
    *(f"{position}_{kind}_open" for position in ("front_left", "front_right", "rear_left", "rear_right")
      for kind in ("door", "window")),
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
    # Nor does a connector lock reading establish a remote unlock.
    "charger_unlock": (),
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
    "charge-schedule": "charge_control", "charger-unlock": "charger_unlock",
    "flash-on": "lights", "flash-off": "lights",
    "ventilate-on": "ventilation", "ventilate-off": "ventilation",
    "horn-on": "horn", "horn-off": "horn", "battery-precondition": "battery_precondition",
    "battery-precondition-off": "battery_precondition",
    "antivirus-on": "sterilisation", "antivirus-off": "sterilisation",
    **{f"seat-{i}-ventilation-{direction}": f"seat_{i}_ventilation"
       for i in range(1, 5) for direction in ("on", "off")},
}
FEATURES = READINGS | EQUIPMENT_READINGS.keys()
# An off reading proves nothing for these: an Aion UT reports a light that is off the
# same way as one that is not fitted, so only a light seen on establishes it.
_SEEN_WHEN_ON = frozenset({"headlights_on", "high_beam_on", "rear_fog_on", "turn_signal_on", "brake_lights_on"})

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
    "sterilisation_on": ("airConditionCapabilities", "airConditionAntivirusModeCapability", _READ),
    "doors": ("doorCapabilities", "lockCapability", _CONTROL),
    "locked": ("doorCapabilities", "lockCapability", _READ),
    "door_open": ("doorCapabilities", "openModeCapability", _READ),
    "windows": ("windowCapabilities", "openModeCapability", _CONTROL),
    "window_open": ("windowCapabilities", "openModeCapability", _READ),
    # The ventilate command sits with the windows in the app's capability model.
    "ventilation": ("windowCapabilities", "ventilateModeCapability", _CONTROL),
    # An Aion UT lists hatch 3 (the boot) controllable and hatch 1 (the bonnet) switched
    # off, though the bonnet is reported; so the bonnet follows the reports alone.
    "tailgate": ("hatchCapabilities", "openModeCapability", _CONTROL),
    "hatch_open": ("hatchCapabilities", "openModeCapability", _READ),
    "sunroof": ("sunroofCapabilities", "openModeCapability", _CONTROL),
    "sunroof_open": ("sunroofCapabilities", "openModeCapability", _READ),
    "lights": ("lightCapabilities", "openCapability", _CONTROL),
    "lights_on": ("lightCapabilities", "openCapability", _READ),
    "horn": ("hornCapabilities", "hootCapability", _CONTROL),
    "brake_fluid_low": ("brakeCapabilities", "alarmsCapability", _READ),
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
    "charger_unlock": ("chargerCapability", "lockCapability", _CONTROL),
    "power_on": ("powerCapabilities", "powerCapability", _READ),
    "vehicle_power_control": ("powerCapabilities", "powerCapability", _CONTROL),
    "cabin_temp_c": ("environmentCapabilities", "temperatureInsideCarCapability", _READ),
    "outside_temp_c": ("environmentCapabilities", "temperatureOutsideCarCapability", _READ),
    "pm25": ("environmentCapabilities", "pm25Capability", _READ),
    "soc": ("drivingOverviewCapabilities", "remainElectricityPercentageCapability", _READ),
    "range_km": ("drivingOverviewCapabilities", "remainVoyageCourseCapability", _READ),
    "odometer_km": ("drivingOverviewCapabilities", "totalVoyageCourseCapability", _READ),
    "aux_voltage": ("batteryCapabilities", "voltageCapability", _READ),
    "battery_temp_c": ("batteryCapabilities", "temperatureCapability", _READ),
    "trip_consumption_kwh_100km": ("drivingOverviewCapabilities", "averageElectricityConsumptionCapability", _READ),
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


def _available(remote: dict, group: str, match: dict[str, Any] | None = None) -> list[dict]:
    """The group's entries (those matching ``match``) that are switched on."""
    return [item for item in remote.get(group) or () if isinstance(item, dict)
            and all(item.get(key) == value for key, value in (match or {}).items())
            and _level(item.get("controlCapability")) >= 1]


def _flag(remote: dict, group: str, field: str, levels: tuple[int, ...],
          match: dict[str, Any] | None = None) -> bool | None:
    """True or False from the list; None when an available group does not mention the field."""
    available = _available(remote, group, match)
    if not available:
        return False                      # not listed, or switched off
    if field != "controlCapability" and not any(field in item for item in available):
        return None
    return any(_level(item.get(field)) in levels for item in available)


def _remote_groups(data: Any) -> dict | None:
    """The first record's remote-capability groups; None for an answer too small to rely on."""
    records = data.get("capabilities") if isinstance(data, dict) else None
    record = records[0] if isinstance(records, list) and records else data
    capability = record.get("capability", record) if isinstance(record, dict) else None
    remote = capability.get("remoteCapability") if isinstance(capability, dict) else None
    if not isinstance(remote, dict) or sum(isinstance(v, list) for v in remote.values()) < _MIN_GROUPS:
        return None
    return remote


def equipment_from_capabilities(data: Any) -> dict[str, bool]:
    """What the car's capability list offers remotely, as equipment flags.

    Takes the response of ``GacClient.get_control_capabilities``. A feature whose
    group is missing or switched off is False; one the list does not mention is
    left out. Returns {} for a response too small to rely on.
    """
    if (remote := _remote_groups(data)) is None:
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


def _choices(items: list[dict], field: str, values) -> tuple[int, ...] | None:
    """``values(item)`` for the first switched-on entry that states ``field``; () when it grants no control.

    None when no entry is switched on (its values are then template defaults) or none mentions the field.
    """
    item = next((item for item in items if field in item), None)
    if item is None:
        return None
    if _level(item[field]) not in _CONTROL:
        return ()
    return values(item)


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _between(low_field: str, high_field: str, accepted: range):
    """The non-zero accepted values within an entry's advertised minimum and maximum."""
    def values(item: dict) -> tuple[int, ...] | None:
        low, high = item.get(low_field), item.get(high_field)
        if type(low) is not int or type(high) is not int:
            return None
        return tuple(value for value in accepted if value and low <= value <= high)
    return values


def _span(low_field: str, high_field: str, bounds: tuple[float, float], per: int = 1):
    """(lowest, highest) whole number within an entry's advertised minimum and maximum and ``bounds``.

    ``per`` converts the advertised unit, for example 60 for seconds to minutes.
    """
    def values(item: dict) -> tuple[int, ...] | None:
        low, high = item.get(low_field), item.get(high_field)
        if not _number(low) or not _number(high):
            return None
        lo = max(math.ceil(low / per), math.ceil(bounds[0]))
        hi = min(math.floor(high / per), math.floor(bounds[1]))
        return (lo, hi) if lo <= hi else ()
    return values


def _codes(field: str, accepted: tuple[int, ...] | None = None):
    """The listed codes this library sends: those in ``accepted``, or else any positive code."""
    def values(item: dict) -> tuple[int, ...] | None:
        codes = item.get(field)
        if not isinstance(codes, list):
            return None
        return tuple(sorted({code for code in codes if type(code) is int
                             and (code in accepted if accepted is not None else code > 0)}))
    return values


_FRIDGE_FIELDS = {"refrigerate": "Refrigeration", "heat": "Heating", "freeze": "Freezing"}


def ranges_from_capabilities(data: Any) -> dict[str, tuple[int, ...]]:
    """The values the car's capability list accepts remotely, within what this library sends.

    Takes the response of ``GacClient.get_control_capabilities``. Choices, as tuples of codes
    or levels: ``fan_speeds`` (manual blower speeds; off is its own command),
    ``blowing_modes`` (outlet codes), ``cycling_modes`` (air-source codes),
    ``seat_<n>_ventilation_levels`` (strengths; 0 is off and always allowed),
    ``battery_precondition_modes`` (0 cancel, 1 start) and ``fridge_modes`` (working-mode
    codes). Spans, as (lowest, highest) whole numbers: ``ac_temperature_range`` (°C),
    ``ac_minutes_range`` and ``fridge_<mode>_temperature_range`` (°C).

    An empty tuple means the list offers no remote choice. A key is left out when the
    list does not say, including for a switched-off group, whose values are template
    defaults. Returns {} for a response too small to rely on. An Aion UT lists fan speeds
    1–7 (its console has ten), outlets 1–4, front-seat levels 1–3 and 18–32 °C, and remote
    trials matched each.
    """
    if (remote := _remote_groups(data)) is None:
        return {}
    air = _available(remote, "airConditionCapabilities")
    fridge = _available(remote, "refrigeratorCapabilities")
    ranges = {
        "fan_speeds": _choices(air, "windStrengthCapability",
                               _between("minWindStrength", "maxWindStrength", FAN_SPEEDS)),
        # An Aion UT also lists outlet 0, whose meaning is unknown.
        "blowing_modes": _choices(air, "blowingModeCapability", _codes("supportBlowingModes")),
        "cycling_modes": _choices(air, "cyclingModeCapability", _codes("supportCyclingModes")),
        "ac_temperature_range": _choices(air, "temperatureCapability",
                                         _span("minTemperature", "maxTemperature", CLIMATE_TEMP_RANGE)),
        # In seconds: an Aion UT lists 60–3600, the app's 1–60 minutes.
        "ac_minutes_range": _choices(air, "airConditionDurationCapability", _span(
            "minAirConditionCurrentDuration", "maxAirConditionCurrentDuration", CLIMATE_MINUTES_RANGE, per=60)),
        "battery_precondition_modes": _choices(
            _available(remote, "batteryPretreatmentCapabilities"), "batteryPretreatmentModeCapability",
            _codes("supportBatteryPretreatmentModes", (0, 1))),
        "fridge_modes": _choices(fridge, "refrigeratorCapability", _codes("workingMode", tuple(FRIDGE_MODES.values()))),
        **{f"fridge_{mode}_temperature_range": _choices(fridge, "refrigeratorCapability", _span(
            f"min{name}Temperature", f"max{name}Temperature", FRIDGE_TEMP_RANGE[mode]))
           for mode, name in _FRIDGE_FIELDS.items()},
    }
    seat_levels = _between("minWindStrength", "maxWindStrength", SEAT_VENTILATION_LEVELS)
    for seat in range(1, 5):
        ranges[f"seat_{seat}_ventilation_levels"] = _choices(
            _available(remote, "seatTemperatureCapabilities", {"seat": seat}), "windStrengthCapability", seat_levels)
    return {key: value for key, value in ranges.items() if value is not None}


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
            value = getattr(status, field, None)
            if field in _SEEN_WHEN_ON and value is not True:
                continue
            if not field.startswith(("tyre_", "seat_")) and _known(value):
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
                         for name in COMMAND_FEATURES},
        }
