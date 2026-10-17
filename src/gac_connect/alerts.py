"""Car alerts from GAC's alert list (``GacClient.get_alarms``, the app's "Vehicle alarm" page).

Each record names an alert by ``alarmSource``/``alarmObject``/``alarmReason``; the app turns
that triple into a title (GAC 2.0.32 function 0x70efe4). Titles here are this library's own
English wording. A record can list several alerts; like the app, the last one is used.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .errors import ResponseError

# (source, object, reason) -> (key, title)
ALERT_TYPES: dict[tuple[int, int, int], tuple[str, str]] = {
    (210, 0, 6): ("charging_finished", "Charging finished"),
    (401, 2, 7): ("charging_finished", "Charging finished"),
    (210, 0, 3): ("connector_not_plugged_in", "Charging time reached, but the connector is not plugged in"),
    (210, 0, 7): ("charging_failed", "Charging failed"),
    (210, 0, 8): ("charging_failed", "Charging failed"),
    (210, 0, 10): ("charger_fault", "Charger fault; charging failed"),
    (302, 14, 1): ("charger_fault", "Charger fault; charging failed"),
    (210, 0, 12): ("full_charge_reminder", "Charge fully now and then so the battery can calibrate"),
    (210, 0, 13): ("full_charge_reminder", "Charge fully now and then so the battery can calibrate"),
    (210, 0, 14): ("battery_self_check_done", "Battery self-check completed"),
    (210, 0, 15): ("power_hold_ended", "Power Hold ended: battery low, the car switched off"),
    (210, 0, 16): ("power_hold_ended", "Power Hold ended: battery low, the car switched off"),
    (203, 4, 1): ("power_hold_on", "Power Hold on"),
    (203, 2, 7): ("battery_temp_not_started", "Smart battery temp: conditions not met"),
    (203, 2, 8): ("battery_temp_running", "Smart battery temp running"),
    (203, 2, 9): ("battery_temp_stopped", "Smart battery temp stopped: battery low or a thermal fault"),
    (203, 2, 10): ("battery_temp_finished", "Smart battery temp finished"),
    (203, 2, 11): ("battery_temp_cancelled", "Smart battery temp cancelled"),
    (203, 0, 2): ("low_12v_battery", "12 V battery low"),
    (203, 1, 7): ("low_12v_battery", "12 V battery low"),
    (140, 3, 1): ("low_12v_battery", "12 V battery low"),
    (401, 3, 9): ("low_12v_battery", "12 V battery low"),
    (203, 0, 3): ("high_12v_voltage", "12 V battery voltage too high"),
    (207, 0, 3): ("tyre_pressure_low", "Tyre pressure low"),
    (207, 0, 6): ("tyre_sensor_fault", "Tyre pressure sensor fault"),
    (101, 0, 3): ("sterilisation_finished", "Sterilisation finished"),
    (101, 0, 4): ("sterilisation_interrupted", "Sterilisation interrupted"),
    (101, 0, 5): ("living_being_ac_on", "Living being detected; A/C switched on"),
    (401, 1, 12): ("living_being_left", "Living being may be left in the car"),
    (208, 0, 1): ("anti_theft", "Anti-theft alarm: possible intrusion"),
    (1, 0, 2): ("door_unlocked", "Doors not locked"),
    (2, 0, 1): ("window_open", "Window open"),
    (2, 0, 2): ("ventilation_windows_closed", "Ventilation mode closed the windows (rain)"),
    (2, 0, 3): ("ventilation_windows_not_closed", "Ventilation mode could not close the windows (rain)"),
    (3, 0, 1): ("sunroof_open", "Sunroof open"),
    (5, 1, 1): ("bonnet_open", "Bonnet open"),
    (5, 2, 1): ("boot_open", "Boot open"),
    (402, 1, 2): ("hazard_lights_on", "Hazard lights left on"),
    (305, 0, 0): ("thermal_fault", "Thermal system fault"),
    (130, 3, 1): ("fridge_switched_off", "Fridge finished and switched off"),
    (130, 3, 2): ("fridge_start_failed", "Fridge could not start: battery low"),
    (130, 3, 3): ("fridge_door_open", "Fridge door open"),
    (130, 3, 4): ("fridge_door_open", "Fridge door open"),
    (130, 3, 5): ("fridge_switched_off_low_battery", "Fridge switched off: battery low"),
    (999, 0, 1): ("fridge_scheduled_start", "Fridge started on schedule"),
    (999, 0, 2): ("fridge_scheduled_start_failed", "Fridge scheduled start failed"),
    (999, 0, 3): ("fridge_low_battery", "Fridge cannot start: battery low"),
}
OTHER = "other"
ALERT_KEYS = tuple(sorted({key for key, _ in ALERT_TYPES.values()} | {OTHER}))


@dataclass(frozen=True)
class Signal:
    code: int | None
    description: str | None
    value: str | None


@dataclass(frozen=True)
class Alert:
    alert_id: str
    created: datetime | None
    source: int | None
    object: int | None
    reason: int | None
    key: str
    title: str
    message: str | None = None          # GAC's own text, often Chinese
    signals: tuple[Signal, ...] = field(default_factory=tuple)


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,9}", value.strip()):
        return int(value)        # plain ASCII digits only ("--1" or "²" are not numbers here)
    return None


def _text(value: Any, limit: int = 200) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_alert(item: Any) -> Alert | None:
    """One record of the alert list, or None without an id."""
    if not isinstance(item, dict) or not (alert_id := _text(item.get("alarmId"), 64)):
        return None
    content = item.get("alarmContent")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            content = None
    statuses = content.get("alarmStatus") if isinstance(content, dict) else None
    status = statuses[-1] if isinstance(statuses, list) and statuses and isinstance(statuses[-1], dict) else {}
    alarms = status.get("alarms")
    alarm = alarms[-1] if isinstance(alarms, list) and alarms and isinstance(alarms[-1], dict) else {}
    triple = (_int(status.get("alarmSource")), _int(alarm.get("alarmObject")), _int(alarm.get("alarmReason")))
    key, title = ALERT_TYPES.get(triple, (OTHER, "Car alert {}/{}/{}".format(*triple)))  # type: ignore[arg-type]
    raw_signals = alarm.get("signals")
    signals = tuple(Signal(_int(s.get("signalCode")), _text(s.get("signalDes")), _text(s.get("signalValue"), 32))
                    for s in (raw_signals if isinstance(raw_signals, list) else ()) if isinstance(s, dict))
    return Alert(alert_id, _time(item.get("createTime")), *triple, key, title,
                 _text(alarm.get("alarmMessage")) or _text(status.get("alarmMessage")), signals)


def parse_alerts(page: Any) -> list[Alert]:
    """The records of one alert page (newest first, as GAC lists them).

    Raises ResponseError for a page that is not an alert list, so a malformed reply is
    never taken for an empty one.
    """
    if not isinstance(page, dict):
        raise ResponseError("the alert list reply is not a page of alerts")
    items = page.get("list")
    if items is None:
        items = []          # an empty page may leave the list out or null
    if not isinstance(items, list):
        raise ResponseError("the alert list reply is not a list of alerts")
    return [alert for item in items if (alert := parse_alert(item)) is not None]
