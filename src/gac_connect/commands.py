"""Vehicle command catalog and request-body assembly.

Each command names a gateway path, a body ``kind``, whether it needs the
remote-control PIN, and whether it is in the ``stable`` support tier (the rest
are best-effort and may not apply to every model). The body shapes:

* ``read`` / ``flat`` → ``{"vin": …, **ops, **overrides}``
* ``ops``            → ``{"identifier": {"vin": …}, "operations": [{…ops, **overrides}]}``
* ``seat``           → ``ops`` with identifier ``grade=2`` and ``duration=30``
* ``air``            → ``ops`` with identifier ``grade=2`` and the requested duration
* ``air-adjust``     → ``ops`` with identifier ``grade=2``, without starting a timed run
* ``flat-ops``       → ``{"vin": …, "operations": [{…ops, **overrides}]}``

PIN-gated commands are listed for completeness but the client refuses them until
PIN support lands.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .const import IOV_PREFIX

_AIR = {"enableAutoMode": 1, "temperature": 24.0, "temperatureUnit": 0}


@dataclass(frozen=True)
class Command:
    name: str
    path: str
    kind: str              # read | flat | ops | seat | air | air-adjust | flat-ops
    pin: bool = False
    stable: bool = False
    category: str = ""
    description: str = ""
    ops: dict = field(default_factory=dict)


def _p(suffix: str) -> str:
    return f"{IOV_PREFIX}/{suffix}"


CATALOG: dict[str, Command] = {c.name: c for c in [
    # reads
    Command("status", _p("get_vehicle_status"), "read", stable=True, category="read", description="full telemetry"),
    Command("position", _p("get_position_status"), "read", stable=True, category="read", description="location"),
    # climate
    Command("aircon-on", _p("air_condition/open"), "air", stable=True, category="climate",
            description="automatic A/C on", ops=_AIR),
    # Verified on an Aion UT: omit blower/outlet fields to retain their settings.
    Command("aircon-manual-on", _p("air_condition/open"), "air", category="climate",
            description="manual A/C on, optionally selecting fan speed 1–10 or fan only",
            ops={"enableAutoMode": 0, "enableAirCompressor": 1,
                 "temperature": 24.0, "temperatureUnit": 0}),
    Command("aircon-off", _p("air_condition/close"), "flat-ops", stable=True, category="climate",
            description="A/C off", ops={"enableAutoMode": 0}),
    # A partial adjustment omits Auto, compressor, blower and airflow fields.
    # Its acceptance must be verified on the vehicle; never fall back to Auto.
    Command("aircon-temperature", _p("air_condition/open"), "air-adjust", category="climate",
            description="adjust running A/C temperature without requesting Auto",
            ops={"temperature": 24.0, "temperatureUnit": 0}),
    # GAC 2.0.32 sterilisation (hot-air antivirus), as the app's dispatcher 0x81cefc sends it:
    # a flat body with grade 2 and a 30-minute run. Off ({"vin"}) stopped an app-started run
    # on an Aion UT; the car then reports airConditionAntivirusMode 0.
    Command("antivirus-on", _p("control_air_condition_antivirus/on"), "flat", category="climate",
            description="cabin sterilisation on", ops={"grade": 2, "duration": 30}),
    Command("antivirus-off", _p("control_air_condition_antivirus/off"), "flat", category="climate",
            description="cabin sterilisation off"),
    Command("ventilate-on", _p("ventilate_mode/open"), "flat", stable=True, category="climate",
            description="cabin ventilation on"),
    Command("ventilate-off", _p("ventilate_mode/close"), "flat", category="climate", description="cabin ventilation off"),
    Command("steering-on", _p("steering/on"), "flat", stable=True, category="climate", description="steering heat on"),
    Command("steering-off", _p("steering/off"), "flat", stable=True, category="climate", description="steering heat off"),
    Command("fridge-on", _p("refrigerator/open"), "ops", category="climate",
            description="fridge on in a mode at a temperature (if fitted)",
            ops={"refrigeratorOperationType": "on", "workingMode": 1, "refrigeratorTemperature": 3.0}),
    Command("fridge-off", _p("refrigerator/close"), "flat", category="climate", description="fridge off"),
    # body
    Command("lock", _p("door/lock"), "flat", stable=True, category="body", description="lock doors"),
    Command("unlock", _p("door/unlock"), "flat", category="body", description="unlock doors (no PIN required)"),
    Command("window-open", _p("window/open"), "flat", category="body", description="windows open"),
    Command("window-close", _p("window/close"), "flat", stable=True, category="body", description="windows close"),
    Command("sunroof-open", _p("sunroof/open"), "flat", category="body", description="sunroof open"),
    Command("sunroof-close", _p("sunroof/close"), "flat", category="body", description="sunroof close"),
    Command("tailgate-open", _p("hatchback/open"), "flat", category="body", description="tailgate open"),
    Command("tailgate-close", _p("hatchback/close"), "flat", category="body", description="tailgate close"),
    Command("flash-on", _p("flash/on"), "flat", category="body", description="flash lights on"),
    Command("flash-off", _p("flash/off"), "flat", category="body", description="flash lights off"),
    Command("horn-on", _p("horn/on"), "flat", category="body", description="sound horn"),
    Command("horn-off", _p("horn/off"), "flat", category="body", description="horn off"),
    # charging & power
    Command("charge-schedule", _p("charging/reservation"), "ops", stable=True, category="charge", description="set charge schedule"),
    # GAC 2.0.32 sends it like door unlock, with only the VIN, after a confirmation (no PIN).
    Command("charger-unlock", _p("charger/unlock"), "flat", category="charge",
            description="unlock the charging connector (no PIN required)"),
    Command("battery-precondition", _p("battery/pretreatment"), "ops", category="charge",
            description="battery preconditioning", ops={"batteryPretreatmentMode": 1}),
    # The app's pretreatment modes: 0 cancel, 1 immediately, 2 once, 3 loop. An Aion UT
    # accepted both requests and reported the mode back.
    Command("battery-precondition-off", _p("battery/pretreatment"), "ops", category="charge",
            description="cancel battery preconditioning", ops={"batteryPretreatmentMode": 0}),
    Command("power-on", _p("engine/start"), "flat", pin=True, category="power", description="remote power on (PIN)"),
    Command("power-off", _p("engine/stop"), "flat", pin=True, category="power", description="remote power off (PIN)"),
    # The app's vehicle-shutdown branch is separate from its engine start/stop commands.
    Command("vehicle-power-off", _p("power/off"), "flat", category="power",
            description="vehicle shutdown (GAC 2.0.32)"),
    # HTTP 404 on the SEA gateway (Aion UT, Thailand): Power Hold is stopped from the console.
    Command("powerkeep-stop", _p("stop/powerKeep"), "flat", category="power", description="stop power-keep"),
    # GAC 2.0.32 selects ventilation with enableWind=1; strength 0 switches it off.
    # Leave temperature fields absent so a seat command does not change cabin A/C.
    *(Command(f"seat-{seat}-ventilation-{direction}", _p("control_seat_temperature"), "seat",
              category="climate", description=f"seat {seat} ventilation {direction}",
              ops={"seat": seat, "enableWind": 1, "windStrength": strength})
      for seat in range(1, 5) for direction, strength in (("on", 1), ("off", 0))),
]}

# What the GAC 2.0.32 app lists when the car refuses a command with 13005 ("conditions
# not met"), chosen by its control type; the wording is this library's own. The app has
# no specific list for charging, sunroof, steering heat or fridge commands, and leaves
# out the range for A/C on in fuel cars.
_STILL = "the car is not moving"
_OFF = "the car is not moving and is switched off"
_LOCKED = "the car is locked"
_RANGE = "the car has enough range"
_NOT_DRIVER_ON = "the car was not switched on from inside"
_HAZARDS_OFF = "the hazard warning lights are off"
_STERILISE = (_OFF, _LOCKED, "the windows and sunroof are closed", _RANGE,
              "the car's software supports sterilisation")
_CONDITIONS: dict[str, tuple[str, ...]] = {
    "door/lock": (_STILL, "all doors are closed", "the bonnet and boot are closed"),
    "door/unlock": (_STILL,),
    "window/open": (_OFF,), "window/close": (_OFF,),
    "ventilate_mode/open": (_OFF,), "ventilate_mode/close": (_OFF,),
    "hatchback/open": (_STILL,), "hatchback/close": (_STILL,),
    "horn/on": (_STILL,), "horn/off": (_STILL,),
    "flash/on": (_STILL, _HAZARDS_OFF), "flash/off": (_STILL, _HAZARDS_OFF),
    "charger/unlock": (_STILL,),
    "air_condition/open": (_OFF, _LOCKED, _RANGE),
    "air_condition/close": (_STILL,),
    "control_air_condition_antivirus/on": _STERILISE, "control_air_condition_antivirus/off": _STERILISE,
    "battery/pretreatment": (_OFF, _LOCKED),
    "control_seat_temperature": (_OFF, _LOCKED, _RANGE),
    "engine/start": (_OFF, _LOCKED, _RANGE),
    "engine/stop": (_STILL, _NOT_DRIVER_ON),
    "power/off": (_STILL, _NOT_DRIVER_ON),
}
# What a car was seen to accept beyond the app's list (Aion UT, 2026-10-17: seat levels were
# refused twice while the car was switched on but not ready, then accepted once the brake
# was pressed; the refusal carried "Anti-theft warning status", a status field, not the cause).
_SEEN: dict[str, str] = {
    "control_seat_temperature": ("With someone inside, it also works with the car ready to drive "
                                 "(press the brake); switched on but not ready, it is refused"),
}


def refusal_hint(code: object, command: str) -> str | None:
    """The app's conditions for a 13005 refusal of a catalog command or gateway path, else None."""
    if type(code) not in (int, str) or str(code) != "13005":
        return None
    cmd = CATALOG.get(command)
    path = (cmd.path if cmd else command).rsplit("/access_gateway/", 1)[-1]
    conditions = _CONDITIONS.get(path)
    if not conditions:
        return None
    hint = f"The GAC app lists these conditions: {'; '.join(conditions)}."
    return f"{hint} {_SEEN[path]}." if path in _SEEN else hint


CLIMATE_TEMP_RANGE = (18.0, 32.0)   # °C

# Fridge / warmer box. The car reports and accepts a working mode and a target
# temperature; each running mode has its own range. "off" is only reported,
# never sent: switching off is its own command.
FRIDGE_MODES = {"refrigerate": 1, "heat": 2, "freeze": 4}
FRIDGE_OFF_CODE = 3
FRIDGE_TEMP_RANGE = {"refrigerate": (0.0, 20.0), "heat": (35.0, 50.0), "freeze": (-15.0, -1.0)}   # °C
FRIDGE_DEFAULT_TEMP = {"refrigerate": 3.0, "heat": 42.0, "freeze": -12.0}
CLIMATE_MINUTES_RANGE = (5, 60)


def validate_climate(temperature: Any, minutes: Any) -> tuple[float, int]:
    """Check A/C inputs before anything is sent to the car; raises ValueError."""
    try:
        t = float(temperature)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("temperature must be a number") from None
    if not math.isfinite(t):
        raise ValueError("temperature must be finite")
    lo, hi = CLIMATE_TEMP_RANGE
    if not lo <= t <= hi:
        raise ValueError(f"temperature must be between {lo:g} and {hi:g} °C")
    try:
        mf = float(minutes)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("minutes must be an integer") from None
    if not math.isfinite(mf) or mf != int(mf):
        raise ValueError("minutes must be a whole number")
    m = int(mf)
    mlo, mhi = CLIMATE_MINUTES_RANGE
    if not mlo <= m <= mhi:
        raise ValueError(f"minutes must be between {mlo} and {mhi}")
    return round(t * 2) / 2, m


FAN_SPEEDS = range(1, 11)   # the console's ten blower speeds; a car's remote range may be smaller


def validate_fan_speed(speed: Any) -> int:
    """Accept the ten manual cabin blower speeds; Off uses climate_off instead."""
    if type(speed) is not int or speed not in FAN_SPEEDS:
        raise ValueError("fan speed must be an integer from 1 to 10")
    return speed


def validate_blowing_mode(mode: Any) -> int:
    """Accept a positive raw outlet code; its meaning is vehicle-specific."""
    if type(mode) is not int or mode <= 0:
        raise ValueError("blowing mode must be a positive integer outlet code")
    return mode


# Verified on an Aion UT: applied only while its A/C already runs; 3 is accepted
# without visible effect, and the console's Auto air source reports as 2.
CYCLING_RECIRCULATE, CYCLING_FRESH_AIR = 1, 2


def validate_cycling_mode(mode: Any) -> int:
    """Accept a positive raw air-source code: 1 recirculate and 2 fresh air on an Aion UT."""
    if type(mode) is not int or mode <= 0:
        raise ValueError("cycling mode must be a positive integer air-source code")
    return mode


def validate_compressor(value: Any) -> int:
    """Accept the compressor flag: 1 cools or heats, 0 runs the blower alone (the console's Fan)."""
    if type(value) not in (bool, int) or value not in (0, 1):
        raise ValueError("compressor must be 1 (on) or 0 (fan only)")
    return int(value)


SEAT_VENTILATION_LEVELS = range(4)   # 0 off, 1–3 strength, verified on an Aion UT's front seats


def validate_seat_level(level: Any) -> int:
    if type(level) is not int or level not in SEAT_VENTILATION_LEVELS:
        raise ValueError("seat ventilation level must be an integer from 0 to 3")
    return level


def build_body(cmd: Command, vin: str, overrides: dict[str, Any] | None = None) -> dict:
    """Assemble a request body; overrides never replace the vehicle identity.

    The body VIN must match the encrypted ``vin`` header, so top-level ``vin``
    and ``identifier`` overrides are ignored.
    """
    overrides = overrides or {}
    if cmd.kind in ("read", "flat"):
        return {**cmd.ops, **overrides, "vin": vin}
    if cmd.kind == "ops":
        return {"identifier": {"vin": vin}, "operations": [{**cmd.ops, **overrides}]}
    if cmd.kind in ("air", "air-adjust"):
        operation = {**cmd.ops, **overrides}
        minutes = operation.pop("currentDuration", 30)
        temperature, duration = validate_climate(operation["temperature"], minutes)
        operation["temperature"] = temperature
        if cmd.name == "aircon-manual-on" and "windStrength" in operation:
            operation["windStrength"] = validate_fan_speed(operation["windStrength"])
        if cmd.name == "aircon-manual-on" and "blowingMode" in operation:
            operation["blowingMode"] = validate_blowing_mode(operation["blowingMode"])
        if cmd.name == "aircon-manual-on" and "cyclingMode" in operation:
            operation["cyclingMode"] = validate_cycling_mode(operation["cyclingMode"])
        if cmd.name == "aircon-manual-on":
            operation["enableAirCompressor"] = validate_compressor(operation["enableAirCompressor"])
        identifier = {"vin": vin, "grade": 2}
        if cmd.kind == "air":
            identifier["duration"] = duration
        return {"identifier": identifier, "operations": [operation]}
    if cmd.kind == "flat-ops":
        return {"vin": vin, "operations": [{**cmd.ops, **overrides}]}
    if cmd.kind == "seat":
        return {"identifier": {"vin": vin, "grade": 2, "duration": 30},
                "operations": [{**cmd.ops, **overrides}]}
    raise ValueError(f"unknown command kind {cmd.kind!r}")


def validate_fridge(mode: Any, temperature: Any = None) -> tuple[int, float]:
    """Check fridge inputs before anything is sent to the car; raises ValueError.

    ``mode`` is "refrigerate", "heat" or "freeze". ``temperature`` is checked
    against that mode's range and then rounded to a whole degree (halves round to
    the even degree). When omitted it defaults to this library's setting for the
    mode — refrigerate 3, heat 42, freeze -12 °C — not a value read from the car.
    """
    if not isinstance(mode, str) or mode not in FRIDGE_MODES:
        raise ValueError(f"mode must be one of {', '.join(FRIDGE_MODES)}")
    if temperature is None:
        temperature = FRIDGE_DEFAULT_TEMP[mode]
    if isinstance(temperature, bool):
        raise ValueError("temperature must be a number")
    try:
        t = float(temperature)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("temperature must be a number") from None
    if not math.isfinite(t):
        raise ValueError("temperature must be finite")
    lo, hi = FRIDGE_TEMP_RANGE[mode]
    if not lo <= t <= hi:
        raise ValueError(f"{mode} temperature must be between {lo:g} and {hi:g} °C")
    return FRIDGE_MODES[mode], float(round(t))
