"""Typed vehicle state, decoded from the gateway's accessor groups.

``get_vehicle_status`` returns ``data.results[0]`` as a bag of ``*Accessors``
lists. This module folds the useful ones into a flat, typed :class:`VehicleStatus`
with the gateway's quirks resolved:

* SoC is reported 0..1, exposed as a percentage;
* a charging current of 1638.0 is the gateway's "unknown" placeholder → None;
* tyre pressure stays in native kPa (let the consumer convert to psi if it wants);
* lock uses the gateway's inverted polarity (0 = locked), decoded to a bool;
* openMode 0 = closed, >0 = open, negative = unknown;
* the A/C is "on" when the compressor flag is set or the fan is running;
* reservation times are milliseconds-of-day on the service's clock, exposed as "HH:MM".

The raw ``results`` dict is retained on ``.raw`` so anything not modelled here is
still reachable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class ChargingStatus(IntEnum):
    NOT_CHARGING = 0
    CHARGING = 2
    UNKNOWN = -1


class ChargingMode(IntEnum):
    FREE = 0        # charge whenever plugged in
    SCHEDULED = 1   # only inside the reservation window
    UNKNOWN = -1


_MAX_CHARGE_MINUTES = 7 * 24 * 60   # maximum accepted charge duration


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _finite(v: Any) -> float | None:
    """A real, finite number, or None (booleans and NaN/inf are not readings)."""
    if isinstance(v, bool):
        return None
    f = _num(v)
    return f if f is not None and math.isfinite(f) else None


def _exact_int(v: Any) -> int | None:
    """An enum code: an integral, finite, non-boolean number, else None."""
    f = _finite(v)
    return int(f) if f is not None and f.is_integer() else None


def _first(res: dict, key: str) -> dict:
    items = res.get(key)
    return items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}


def _items(res: dict, key: str) -> list:
    items = res.get(key)
    return items if isinstance(items, list) else []


def _decode_door_locks(doors: Any, model: str | None = None) -> bool | None:
    if not isinstance(doors, list) or not doors:
        return None
    codes = [_exact_int(door.get("lock")) if isinstance(door, dict) else None for door in doors]
    if 1 in codes:
        return False
    if all(code == 0 for code in codes):
        return True
    # Verified UT reports keep rear locks at -1 while the front locks change.
    # Require all four distinct door IDs and an explicitly selected UT model.
    model_name = (model or "").strip().casefold()
    if model_name == "aion ut" or model_name.startswith("aion ut "):
        by_door = {
            door["door"]: code for door, code in zip(doors, codes, strict=True)
            if isinstance(door, dict) and type(door.get("door")) is int
        }
        if len(doors) == 4 and by_door == {1: 0, 2: 0, 3: -1, 4: -1}:
            return True
    return None


def doors_locked(status: VehicleStatus | None, model: str | None = None) -> bool | None:
    """Interpret lock readings with optional, explicitly selected model knowledge.

    Unknown or partial reports stay unknown. The verified Aion UT rear-lock
    sentinel is accepted only for that model and the complete four-door pattern.
    """
    return None if status is None else _decode_door_locks(status.raw.get("doorAccessors"), model)


def _enabled(item: dict, flag: str) -> bool:
    value = item.get(flag, True)
    return value if isinstance(value, bool) else _exact_int(value) == 1


def _bounded(value: Any, low: float, high: float = math.inf) -> float | None:
    number = _num(value)
    return number if number is not None and low <= number <= high else None


def _hhmm(ms: Any) -> str | None:
    """Milliseconds-of-day -> "HH:MM"; None outside 0 <= ms < 24 h."""
    v = _num(ms)
    if v is None or not 0 <= v < 86_400_000:
        return None
    minutes = int(v) // 60000
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass
class Tyre:
    position: str
    pressure_kpa: float | None
    temperature_c: float | None


@dataclass
class VehicleStatus:
    online: bool | None = None
    updated_ms: int | None = None
    # battery / charging
    soc: float | None = None                 # %
    range_km: float | None = None
    charging: bool | None = None
    charging_mode: ChargingMode | None = None
    charge_current_a: float | None = None
    charge_voltage_v: float | None = None
    charge_power_kw: float | None = None
    estimated_charge_minutes: int | None = None
    plugged_in: bool | None = None
    charger_locked: bool | None = None
    # odometer / environment
    odometer_km: float | None = None
    cabin_temp_c: float | None = None
    pm25: float | None = None
    aux_voltage: float | None = None         # 12 V battery
    # location
    latitude: float | None = None
    longitude: float | None = None
    # body
    locked: bool | None = None               # True = all doors locked
    door_open: bool | None = None
    window_open: bool | None = None
    hatch_open: bool | None = None
    sunroof_open: bool | None = None
    tyres: list[Tyre] = field(default_factory=list)
    # climate
    ac_on: bool | None = None
    ac_target_temp_c: float | None = None
    steering_heat_on: bool | None = None
    lights_on: bool | None = None
    # fridge / warmer box: None when the car does not report one
    fridge_fitted: bool = False
    fridge_mode: str | None = None           # "off" | "refrigerate" | "heat" | "freeze"
    fridge_temp_c: float | None = None       # target, reported only while running
    # what the fridge does once you leave the car: "timed" or "unlimited"
    fridge_keep_mode: str | None = None
    # remaining minutes the service reports for a timed setting; None when unavailable,
    # invalid, or the setting is unlimited or unknown
    fridge_keep_minutes: int | None = None
    # the charge reservation as the service reports it (time of day on the
    # service's clock, which need not match the car's local time)
    charge_window_start: str | None = None
    charge_window_stop: str | None = None
    charge_weekly: int | None = None
    raw: dict = field(default_factory=dict)

    # gateway "unknown" placeholder for charge current
    _CURRENT_UNKNOWN = 1638.0
    # doorAccessors[].lock: 0 = locked, 1 = unlocked, <0 = unknown
    _LOCK_LOCKED = 0
    # refrigeratorAccessors[].workingMode
    _FRIDGE_MODE = {1: "refrigerate", 2: "heat", 3: "off", 4: "freeze"}
    _FRIDGE_KEEP = {1: "timed", 0: "unlimited"}

    @classmethod
    def from_results(cls, results: dict, *, online: bool | None = None,
                     updated_ms: int | None = None, model: str | None = None) -> VehicleStatus:
        if not isinstance(results, dict):
            raise ValueError("vehicle report must be an object")
        drv = _first(results, "drivingOverviewAccessors")
        chg = _first(results, "chargingAccessors")
        cgr = _first(results, "chargerAccessors")
        env = _first(results, "environmentAccessors")
        bat = _first(results, "batteryAccessors")
        pos = _first(results, "positionAccessors")
        air = _first(results, "airConditionAccessors")
        steer = _first(results, "steeringAccessors")
        light = _first(results, "lightAccessors")
        fridge = _first(results, "refrigeratorAccessors")
        keep_mode = cls._FRIDGE_KEEP.get(_exact_int(fridge.get("leavingVehicleStatus")))
        keep_minutes = _exact_int(fridge.get("surplusTime")) if keep_mode == "timed" else None
        if keep_minutes is not None and keep_minutes < 0:
            keep_minutes = None

        soc = _bounded(drv.get("remainElectricityPercentage"), 0, 1)
        cur = _bounded(chg.get("chargingCurrent"), 0)
        if cur == cls._CURRENT_UNKNOWN:
            cur = None
        est = _num(chg.get("estimatedChargedDuration"))
        if est is not None and not 0 <= est < _MAX_CHARGE_MINUTES:
            est = None   # discard an out-of-range duration
        volt = _num(chg.get("chargingVoltage")) if _enabled(chg, "chargingVoltageFlag") else None
        if volt is not None and volt <= 0:
            volt = None
        power = round(volt * cur / 1000, 2) if volt is not None and cur is not None and cur >= 0 else None

        locked = _decode_door_locks(results.get("doorAccessors"), model)

        # Stable API IDs, not an unverified mapping to physical corners.
        tyres = [Tyre(str(index), None, None) for index in range(1, 5)]
        seen: set[int] = set()
        for item in _items(results, "tyreAccessors"):
            if not isinstance(item, dict):
                continue
            index = _exact_int(item.get("tyre"))
            if index not in (1, 2, 3, 4):
                continue
            if index in seen:
                tyres[index - 1] = Tyre(str(index), None, None)
                continue
            seen.add(index)
            pressure = _num(item.get("pressure"))
            temperature = _num(item.get("temperature"))
            if pressure == -1 and temperature == -1:
                temperature = None  # paired UT placeholders; -1 C alone is valid
            if not _enabled(item, "pressureFlag") or (pressure is not None and pressure < 0):
                pressure = None
            if not _enabled(item, "temperatureFlag"):
                temperature = None
            tyres[index - 1] = Tyre(str(index), pressure, temperature)

        def any_open(key: str, sub: str) -> bool | None:
            items = _items(results, key)
            modes = [_exact_int(i.get(sub)) if isinstance(i, dict) else None for i in items]
            if any(mode is not None and mode > 0 for mode in modes):
                return True
            return False if modes and all(mode == 0 for mode in modes) else None

        # Negative values are "unknown / not fitted" sentinels. A/C is on when the
        # compressor or the fan reports on; off only when both explicitly report off.
        compressor = _num(air.get("enableAirCompressor"))
        fan = _num(air.get("windStrength"))
        known = [v for v in (compressor, fan) if v is not None and v >= 0]
        if not known:
            ac_on = None
        elif compressor == 1 or (fan is not None and fan > 0):
            ac_on = True
        else:
            ac_on = False if len(known) == 2 else None
        steering = _num(steer.get("steering"))
        if steering is not None and steering < 0:
            steering = None
        lights = _num(light.get("open"))
        if lights is not None and lights < 0:
            lights = None
        weekly = _exact_int(chg.get("weeklyReservation"))
        if weekly is not None and not 0 <= weekly <= 127:
            weekly = None
        mode = _exact_int(chg.get("chargingMode"))
        stamp = _exact_int(updated_ms)
        if stamp is not None and not 0 < stamp < 253402300800000:
            stamp = None

        return cls(
            online=online if isinstance(online, bool) else {0: False, 1: True}.get(_exact_int(online)),
            updated_ms=stamp,
            soc=round(soc * 100, 1) if soc is not None else None,
            range_km=_bounded(drv.get("remainVoyageCourse"), 0),
            charging={0: False, 2: True}.get(_exact_int(chg.get("chargingStatus"))),
            charging_mode=ChargingMode(mode) if mode in (0, 1) else None,
            charge_current_a=cur,
            charge_voltage_v=volt,
            charge_power_kw=power,
            estimated_charge_minutes=int(est) if est is not None else None,
            plugged_in={0: False, 1: True}.get(_exact_int(cgr.get("plug"))),
            charger_locked={0: True, 1: False}.get(_exact_int(cgr.get("lock"))),
            odometer_km=_bounded(drv.get("totalVoyageCourse"), 0),
            cabin_temp_c=_num(env.get("temperatureInsideCar")),
            pm25=_bounded(env.get("pm25"), 0),
            aux_voltage=_bounded(bat.get("voltage"), 0),
            latitude=_bounded(pos.get("latitude"), -90, 90),
            longitude=_bounded(pos.get("longitude"), -180, 180),
            locked=locked,
            door_open=any_open("doorAccessors", "openMode"),
            window_open=any_open("windowAccessors", "openMode"),
            hatch_open=any_open("hatchAccessors", "openMode"),
            sunroof_open=any_open("sunroofAccessors", "openMode"),
            tyres=tyres,
            ac_on=ac_on,
            ac_target_temp_c=_num(air.get("temperature")),
            steering_heat_on=(steering > 0) if steering is not None else None,
            lights_on=(lights > 0) if lights is not None else None,
            fridge_fitted=bool(fridge),
            fridge_mode=cls._FRIDGE_MODE.get(_exact_int(fridge.get("workingMode"))),
            fridge_temp_c=_finite(fridge.get("refrigeratorTemperature")),
            fridge_keep_mode=keep_mode,
            fridge_keep_minutes=keep_minutes,
            charge_window_start=_hhmm(chg.get("dailyReservationStartTime")),
            charge_window_stop=_hhmm(chg.get("dailyReservationStopTime")),
            charge_weekly=int(weekly) if weekly is not None else None,
            raw=results,
        )


@dataclass
class Vehicle:
    vin: str
    plate: str | None = None
    model: str | None = None
    color: str | None = None
    default: bool = False

    @classmethod
    def from_record(cls, rec: dict) -> Vehicle:
        return cls(
            vin=rec.get("vin") or rec.get("vinCode") or "",
            plate=rec.get("plateNo"),
            model=rec.get("vehStyleName") or rec.get("vehicleType"),
            color=rec.get("vehicleColor"),
            default=bool(rec.get("defaultFlag")),
        )
