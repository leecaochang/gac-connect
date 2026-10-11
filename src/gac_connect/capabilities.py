"""Passive equipment discovery from decoded reports and confirmed user actions.

Missing reports never prove absence. Reading a state does not prove that a
remote command works. This module performs no I/O and never probes the vehicle.
Callers may provide explicit equipment flags only from a verified source; the
current client does not fetch an equipment endpoint.
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
    "soc", "range_km", "odometer_km", "cabin_temp_c", "aux_voltage", "pm25",
    "charge_current_a", "charge_voltage_v", "charge_power_kw", "estimated_charge_minutes",
    "charge_window_start", "charge_window_stop", "charge_weekly", "charging_mode", "updated_ms",
    "plugged_in", "charging", "locked", "door_open", "window_open", "hatch_open", "online",
    "charger_locked", "sunroof_open", "steering_heat_on", "lights_on", "ac_on", "ac_target_temp_c",
    "fridge_mode", "fridge_temp_c", "fridge_keep_mode", "fridge_keep_minutes", "latitude", "longitude",
    *(f"tyre_{i}_{kind}" for i in range(1, 5) for kind in ("pressure", "temperature")),
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
    "ventilation": (), "horn": (), "battery_precondition": (),
}
COMMAND_FEATURES = {
    "aircon-on": "climate", "aircon-off": "climate",
    "fridge-on": "fridge", "fridge-off": "fridge",
    "steering-on": "steering_heat", "steering-off": "steering_heat",
    "sunroof-open": "sunroof", "sunroof-close": "sunroof",
    "window-open": "windows", "window-close": "windows",
    "tailgate-open": "tailgate", "tailgate-close": "tailgate",
    "lock": "doors", "unlock": "doors",
    "charge-now": "charge_control", "charge-window": "charge_control", "charge-schedule": "charge_control",
    "flash-on": "lights", "flash-off": "lights",
    "ventilate-on": "ventilation", "ventilate-off": "ventilation",
    "horn-on": "horn", "horn-off": "horn", "battery-precondition": "battery_precondition",
}
FEATURES = READINGS | EQUIPMENT_READINGS.keys()


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
            if not field.startswith("tyre_") and _known(getattr(status, field, None)):
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
        return before != self.export_state()

    def observe_equipment(self, flags: dict[str, bool]) -> bool:
        """Accept only explicit booleans for known equipment from a verified source."""
        before = dict(self._equipment)
        self._equipment.update({key: value for key, value in flags.items()
                                if key in EQUIPMENT_READINGS and isinstance(value, bool)})
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
