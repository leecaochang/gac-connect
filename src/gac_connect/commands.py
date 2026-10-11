"""Vehicle command catalog and request-body assembly.

Each command names a gateway path, a body ``kind``, whether it needs the
remote-control PIN, and whether it is in the ``stable`` support tier (the rest
are best-effort and may not apply to every model). The body shapes:

* ``read`` / ``flat`` → ``{"vin": …, **overrides}``
* ``ops``            → ``{"identifier": {"vin": …}, "operations": [{…ops, **overrides}]}``
* ``seat``           → ``ops`` with identifier ``grade=2`` and ``duration=30``
* ``anti``           → ``{"identifier": {"vin": …}, "currentDuration": 30, **overrides}``

PIN-gated commands are listed for completeness but the client refuses them until
PIN support lands.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .const import IOV_PREFIX

_AIR = {"enableAutoMode": 1, "temperature": 24.0, "temperatureUnit": 0, "currentDuration": 30,
        "blowingMode": -1, "cyclingMode": -1, "enableAirCompressor": -1}


@dataclass(frozen=True)
class Command:
    name: str
    path: str
    kind: str              # read | flat | ops | seat | anti
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
    Command("aircon-on", _p("air_condition/open"), "ops", stable=True, category="climate",
            description="A/C on", ops={**_AIR, "airOperationType": "on"}),
    Command("aircon-off", _p("air_condition/open"), "ops", stable=True, category="climate",
            description="A/C off", ops={**_AIR, "airOperationType": "off"}),
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
    Command("charger-unlock", _p("charger/unlock"), "flat", pin=True, category="charge",
            description="release charging gun / stop charging (PIN)"),
    Command("battery-precondition", _p("battery/pretreatment"), "ops", category="charge",
            description="battery preconditioning", ops={"batteryPretreatmentMode": 1}),
    Command("power-on", _p("engine/start"), "flat", pin=True, category="power", description="remote power on (PIN)"),
    Command("power-off", _p("engine/stop"), "flat", pin=True, category="power", description="remote power off (PIN)"),
    # The app's vehicle-shutdown branch is separate from its engine start/stop commands.
    Command("vehicle-power-off", _p("power/off"), "flat", category="power",
            description="vehicle shutdown (GAC 2.0.32)"),
    Command("powerkeep-stop", _p("stop/powerKeep"), "flat", stable=True, category="power", description="stop power-keep"),
    # GAC 2.0.32 selects ventilation with enableWind=1; strength 0 switches it off.
    # Leave temperature fields absent so a seat command does not change cabin A/C.
    *(Command(f"seat-{seat}-ventilation-{direction}", _p("control_seat_temperature"), "seat",
              category="climate", description=f"seat {seat} ventilation {direction}",
              ops={"seat": seat, "enableWind": 1, "windStrength": strength})
      for seat in range(1, 5) for direction, strength in (("on", 1), ("off", 0))),
]}


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


def build_body(cmd: Command, vin: str, overrides: dict[str, Any] | None = None) -> dict:
    overrides = overrides or {}
    if cmd.kind in ("read", "flat"):
        return {"vin": vin, **overrides}
    if cmd.kind == "anti":
        return {"identifier": {"vin": vin}, "currentDuration": 30, **overrides}
    if cmd.kind == "ops":
        return {"identifier": {"vin": vin}, "operations": [{**cmd.ops, **overrides}]}
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
