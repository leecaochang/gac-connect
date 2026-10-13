"""Typed vehicle state, decoded from the gateway's accessor groups.

``get_vehicle_status`` returns ``data.results[0]`` as a bag of ``*Accessors``
lists. This module folds the useful ones into a flat, typed :class:`VehicleStatus`
with the gateway's quirks resolved:

* SoC is reported 0..1, exposed as a percentage;
* a charging current of 1638.0 or more is the gateway's "unknown" placeholder
  (0x3FFF × 0.1 = 1638.3) → None;
* an odometer of 0 is a placeholder, not a reading → None;
* tyre pressure stays in native kPa (let the consumer convert to psi if it wants);
* lock uses the gateway's inverted polarity (0 = locked), decoded to a bool;
* openMode 0 = closed, >0 = open, negative = unknown;
* the A/C is "on" when the compressor flag is set or the fan is running;
* powerMode 0 = off and 2 = on, verified with Aion UT reports; other codes stay unknown;
* current, charge time, range, cabin temperature and openings whose ``…Flag``
  companion is explicitly 0/false are not valid → None (a missing flag counts as valid);
* chargingMode follows the app: 0 immediately, 1 once, 2 loop;
* reservations can use calendar fields, Unix milliseconds, or milliseconds-of-day;
  explicit calendar fields take precedence over timestamp clock conversions.

The raw ``results`` dict is retained on ``.raw`` so anything not modelled here is
still reachable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ChargingStatus(IntEnum):
    NOT_CHARGING = 0
    CHARGING = 2
    UNKNOWN = -1


class ChargingMode(IntEnum):
    FREE = 0        # charge whenever plugged in (the app's "immediately")
    SCHEDULED = 1   # only inside the reservation window (the app's "once")
    LOOP = 2        # inside the window on the selected days (the app's "loop")
    UNKNOWN = -1

    @property
    def scheduled(self) -> bool:
        return self in (ChargingMode.SCHEDULED, ChargingMode.LOOP)


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


def _not_invalid(item: dict, flag: str) -> bool:
    """False only for an explicit 0/false validity flag; other or missing values do not hide a reading."""
    value = item.get(flag)
    return value is not False and not (type(value) in (int, float) and value == 0)


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


def _calendar_clock(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    parts = {key: _exact_int(value.get(key)) for key in ("year", "month", "day", "hour", "minute")}
    if any(part is None for part in parts.values()):
        return None
    try:
        return datetime(**parts).strftime("%H:%M")
    except (TypeError, ValueError, OverflowError):
        return None


def _timestamp_clock(value: Any, zone: Any) -> str | None:
    stamp = _exact_int(value)
    if stamp is None or not 86_400_000 <= stamp < 253402300800000 or not isinstance(zone, str):
        return None
    try:
        return datetime.fromtimestamp(stamp / 1000, ZoneInfo(zone)).strftime("%H:%M")
    except (ValueError, OverflowError, OSError, ZoneInfoNotFoundError):
        return None


def _reservation_format(chg: dict) -> str | None:
    values = [_exact_int(chg.get(key)) for key in ("dailyReservationStartTime", "dailyReservationStopTime")]
    if all(value is not None and 0 <= value < 86_400_000 for value in values):
        return "time_of_day"
    if all(value is not None and 86_400_000 <= value < 253402300800000 for value in values):
        return "timestamp"
    return None


@dataclass
class Tyre:
    position: str
    pressure_kpa: float | None
    temperature_c: float | None


@dataclass
class Seat:
    """Stable app seat ID: driver, front passenger, rear left, rear right (1–4)."""

    position: str
    ventilation_on: bool | None = None
    ventilation_strength: float | None = None
    heating_strength: float | None = None
    temperature_mode: int | None = None
    # Retain uncalibrated values (potentially Smart) without assigning a meaning.
    ventilation_enable_code: int | None = None


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
    outside_temp_c: float | None = None
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
    ac_fan_strength: float | None = None
    ac_auto_on: bool | None = None
    ac_compressor_on: bool | None = None
    ac_blowing_mode: int | None = None
    ac_cycling_mode: int | None = None
    seats: list[Seat] = field(default_factory=list)
    anti_theft_on: bool | None = None
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
    charge_end_type: int | None = None
    charge_time_format: str | None = None
    charge_clock_consistent: bool | None = None
    raw: dict = field(default_factory=dict)
    # Vehicle power is independent of cloud connectivity and cabin A/C.
    power_on: bool | None = None
    power_mode: int | None = None

    # gateway "unknown" placeholder for charge current: a saturated 14-bit field
    # (0x3FFF × 0.1 = 1638.3), so anything from 1638 A up is not a reading
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
        theft = _first(results, "antiTheftAccessors")
        vehicle_power = _exact_int(_first(results, "powerAccessors").get("powerMode"))
        fridge = _first(results, "refrigeratorAccessors")
        keep_mode = cls._FRIDGE_KEEP.get(_exact_int(fridge.get("leavingVehicleStatus")))
        keep_minutes = _exact_int(fridge.get("surplusTime")) if keep_mode == "timed" else None
        if keep_minutes is not None and keep_minutes < 0:
            keep_minutes = None

        soc = _bounded(drv.get("remainElectricityPercentage"), 0, 1)
        cur = _bounded(chg.get("chargingCurrent"), 0) if _not_invalid(chg, "chargingCurrentFlag") else None
        if cur is not None and cur >= cls._CURRENT_UNKNOWN:
            cur = None
        est = _num(chg.get("estimatedChargedDuration")) if _not_invalid(chg, "estimatedChargedDurationFlag") else None
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
            modes = [_exact_int(i.get(sub)) if isinstance(i, dict) and _not_invalid(i, f"{sub}Flag") else None
                     for i in items]
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

        seats = [Seat(str(index)) for index in range(1, 5)]
        seen_seats: set[int] = set()
        for item in _items(results, "seatTemperatureAccessors"):
            if not isinstance(item, dict):
                continue
            index = _exact_int(item.get("seat"))
            if index not in (1, 2, 3, 4):
                continue
            if index in seen_seats:
                seats[index - 1] = Seat(str(index))
                continue
            seen_seats.add(index)
            wind_code = _exact_int(item.get("enableWind"))
            enabled = {0: False, 1: True}.get(wind_code)
            strength = _bounded(item.get("windStrength"), 0)
            if enabled is True and strength is not None:
                enabled = strength > 0
            seats[index - 1] = Seat(
                str(index), enabled, strength, _bounded(item.get("temperatureStrength"), 0),
                _exact_int(item.get("temperatureMode")), wind_code,
            )

        timezone = results.get("timezoneAccessor")
        zone = (timezone if isinstance(timezone, dict) else _first(results, "timezoneAccessor")).get("timezone")
        clocks = []
        comparisons = []
        for struct_key, time_key in (("startTime", "dailyReservationStartTime"),
                                     ("stopTime", "dailyReservationStopTime")):
            calendar = _calendar_clock(chg.get(struct_key))
            timestamp = _timestamp_clock(chg.get(time_key), zone)
            clocks.append(calendar or _hhmm(chg.get(time_key)) or timestamp)
            if calendar is not None and timestamp is not None:
                comparisons.append(calendar == timestamp)
        end_type = _exact_int(chg.get("chargingEndType"))
        range_km = _bounded(drv.get("remainVoyageCourse"), 0) if _not_invalid(drv, "remainVoyageCourseFlag") else None
        cabin_temp = _num(env.get("temperatureInsideCar")) if _not_invalid(env, "temperatureInsideCarFlag") else None

        return cls(
            online=online if isinstance(online, bool) else {0: False, 1: True}.get(_exact_int(online)),
            updated_ms=stamp,
            power_on={0: False, 2: True}.get(vehicle_power),
            power_mode=vehicle_power,
            soc=round(soc * 100, 1) if soc is not None else None,
            range_km=range_km,
            charging={0: False, 2: True}.get(_exact_int(chg.get("chargingStatus"))),
            charging_mode=ChargingMode(mode) if mode in (0, 1, 2) else None,
            charge_current_a=cur,
            charge_voltage_v=volt,
            charge_power_kw=power,
            estimated_charge_minutes=int(est) if est is not None else None,
            plugged_in={0: False, 1: True}.get(_exact_int(cgr.get("plug"))),
            charger_locked={0: True, 1: False}.get(_exact_int(cgr.get("lock"))),
            # A real odometer is never 0; a zero placeholder would look like a meter reset.
            odometer_km=_bounded(drv.get("totalVoyageCourse"), 0) or None,
            cabin_temp_c=cabin_temp,
            outside_temp_c=_num(env.get("temperatureOutsideCar")),
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
            ac_fan_strength=_bounded(air.get("windStrength"), 0),
            ac_auto_on={0: False, 1: True}.get(_exact_int(air.get("enableAutoMode"))),
            ac_compressor_on={0: False, 1: True}.get(_exact_int(air.get("enableAirCompressor"))),
            ac_blowing_mode=_exact_int(air.get("blowingMode")),
            ac_cycling_mode=_exact_int(air.get("cyclingMode")),
            seats=seats,
            anti_theft_on={0: False, 1: True}.get(_exact_int(theft.get("enable"))),
            steering_heat_on=(steering > 0) if steering is not None else None,
            lights_on=(lights > 0) if lights is not None else None,
            fridge_fitted=bool(fridge),
            fridge_mode=cls._FRIDGE_MODE.get(_exact_int(fridge.get("workingMode"))),
            fridge_temp_c=_finite(fridge.get("refrigeratorTemperature")),
            fridge_keep_mode=keep_mode,
            fridge_keep_minutes=keep_minutes,
            charge_window_start=clocks[0],
            charge_window_stop=clocks[1],
            charge_weekly=int(weekly) if weekly is not None else None,
            charge_end_type=end_type if end_type in (0, 1) else None,
            charge_time_format=_reservation_format(chg),
            charge_clock_consistent=(False if False in comparisons else True if len(comparisons) == 2 else None),
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
