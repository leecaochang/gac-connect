"""Alert records as an Aion UT's account returned them (2026-10-16), VIN replaced."""
from datetime import datetime, UTC

from gac_connect.alerts import ALERT_KEYS, OTHER, parse_alert, parse_alerts

CONTENT = ('{"updateTime": 1791967040601, "identifier": {"vin": "LGXC**********001", "din": "LGXCE4CB0SA000001"}, '
           '"alarmStatus": [{"alarmSource": 210, "alarms": [{"businessAlarmReason": null, "alarmObject": 0, '
           '"alarmReason": 3, "alarmMessage": "\\u5145\\u7535\\u67aa\\u672a\\u63d2\\u5165", '
           '"signals": [{"signalDes": "x", "signalCode": 49, "signalValue": "2.0"}]}], "alarmMessage": "m"}], '
           '"protocol": 1}')
PAGE = {"pageNo": 1, "pageSize": 20, "totalCount": 3, "list": [
    {"alarmId": "a1", "vin": "enc", "createTime": "2026-10-14T08:37:20Z", "alarmType": None, "alarmContent": CONTENT},
    {"alarmId": "a2", "createTime": "2026-09-25T10:12:28Z",
     "alarmContent": {"alarmStatus": [{"alarmSource": 210, "alarms": [{"alarmObject": 0, "alarmReason": 6}]}]}},
    {"alarmId": "a3", "alarmContent": "not json"},
    {"alarmContent": CONTENT},                                       # no id: skipped
]}


def test_the_alert_page_reads_into_typed_alerts():
    first, second, third = parse_alerts(PAGE)
    assert (first.alert_id, first.key, (first.source, first.object, first.reason)) == ("a1", "connector_not_plugged_in",
                                                                                      (210, 0, 3))
    assert first.created == datetime(2026, 10, 14, 8, 37, 20, tzinfo=UTC)
    assert first.message == "充电枪未插入" and first.signals[0].code == 49 and first.signals[0].value == "2.0"
    assert (second.key, second.title) == ("charging_finished", "Charging finished")
    assert third.key == OTHER and third.title == "Car alert None/None/None" and third.created is None


def test_unknown_alerts_keep_their_codes():
    alert = parse_alert({"alarmId": "x", "alarmContent": {"alarmStatus": [
        {"alarmSource": 777, "alarms": [{"alarmObject": 1, "alarmReason": 2}]}]}})
    assert (alert.key, alert.title) == (OTHER, "Car alert 777/1/2")
    assert OTHER in ALERT_KEYS and "living_being_left" in ALERT_KEYS
