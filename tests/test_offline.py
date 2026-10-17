"""Offline unit tests: model decoding, command bodies, charge levers, session,
and that the client constructs. No network, no secrets beyond _material.pem."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def test_imports():
    import gac_connect  # noqa: F401
    from gac_connect import client, commands, models, vehicle, auth, session  # noqa: F401


def test_iov_response_keypair():
    """intl decrypts responses with its iov key; the UK profile decrypts them with
    its own response keypair, while encrypting requests with its own iov key."""
    from gac_connect.crypto import decrypt_envelope, encrypt_envelope
    from gac_connect.keys import load_material
    try:
        intl = load_material("_material.pem")
        uk = load_material("_material_uk.pem")
    except RuntimeError as exc:
        pytest.skip(str(exc))
    # Default profile: one keypair both ways.
    assert intl.iov_response is intl.iov
    # UK: request and response keys are both UK-specific and distinct.
    assert uk.iov.public_pem != intl.iov.public_pem
    assert uk.iov_response.public_pem != uk.iov.public_pem
    # A response encrypted to the UK response key opens with it, and not with the
    # UK request key (the two are distinct keypairs).
    wrapper, _, _ = encrypt_envelope({"ok": True}, uk.iov_response.public)
    assert decrypt_envelope(wrapper, uk.iov_response.private) == {"ok": True}
    assert decrypt_envelope(wrapper, uk.iov.private) is None


def test_uk_has_its_own_iov_hmac():
    """UK signs IoV requests with its own HMAC secret, not GAC International's:
    the shared gateway validates the signature per the IoV app id (GB runs
    under app id 3, intl under 2), so the two apps' signing secrets and request
    keypairs both differ."""
    from gac_connect.keys import load_material
    try:
        intl = load_material("_material.pem")
        uk = load_material("_material_uk.pem")
    except RuntimeError as exc:
        pytest.skip(str(exc))
    assert uk.iov_hmac != intl.iov_hmac
    assert uk.iov_hmac == b"uk-intl-pro"
    # The UK app also encrypts the request body with its own iov keypair.
    assert uk.iov.public_pem != intl.iov.public_pem


def test_uk_iov_keypair_fingerprints():
    """Pin the UK IoV keypairs by SPKI fingerprint, so a future bundle edit can't
    silently fall back to inheriting GAC International's iov key."""
    import hashlib

    from cryptography.hazmat.primitives import serialization
    from gac_connect.keys import load_material

    def spki(keypair) -> str:
        der = keypair.public.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return hashlib.sha256(der).hexdigest()

    try:
        uk = load_material("_material_uk.pem")
    except RuntimeError as exc:
        pytest.skip(str(exc))
    assert spki(uk.iov) == "199a8aeab8f6bdcf4a162ba52e4a89d261239786b7dd874956009dd899b8edb2"
    assert spki(uk.iov_response) == "bc502d7565e9e55a901d15c5516d52adf5d9570f592fdc62fa7eee5a536ba098"
    assert uk.iov_hmac == b"uk-intl-pro"


def test_uk_iov_request_sealed_to_response_key_with_192_bit_aes():
    """UK IoV requests are sealed to the iov_response keypair, the same one its
    responses decrypt with, using a 24-byte (AES-192) session key instead of the
    usual 32-byte (AES-256) one. Other profiles are unaffected: a default
    profile's iov_response is the same object as iov, and its AES key size for
    a freshly drawn request envelope is still 32 bytes."""
    from gac_connect.const import PROFILE_IOV_REQUEST_KEY_SIZE
    from gac_connect.crypto import decrypt_envelope, encrypt_envelope
    from gac_connect.keys import load_material

    try:
        intl = load_material("_material.pem")
        uk = load_material("_material_uk.pem")
    except RuntimeError as exc:
        pytest.skip(str(exc))

    assert PROFILE_IOV_REQUEST_KEY_SIZE.get("uk") == 24
    assert PROFILE_IOV_REQUEST_KEY_SIZE.get("intl", 32) == 32

    wrapper, key, _ = encrypt_envelope(
        {"ticket": "x"}, uk.iov_response.public, key_size=PROFILE_IOV_REQUEST_KEY_SIZE.get("uk", 32)
    )
    assert len(key) == 24
    assert decrypt_envelope(wrapper, uk.iov_response.private) == {"ticket": "x"}

    _, intl_key, _ = encrypt_envelope({"ticket": "x"}, intl.iov_response.public)
    assert len(intl_key) == 32


def test_status_decoding():
    from gac_connect.models import ChargingMode, VehicleStatus
    results = {
        "drivingOverviewAccessors": [{"remainElectricityPercentage": 0.89,
                                      "remainVoyageCourse": 454.0, "totalVoyageCourse": 3205.0}],
        "chargingAccessors": [{"chargingStatus": 2, "chargingMode": 1,
                               "chargingCurrent": 1638.0, "estimatedChargedDuration": 0}],
        "chargerAccessors": [{"plug": 1, "lock": 0}],
        "environmentAccessors": [{"temperatureInsideCar": 30.0, "pm25": 0}],
        "batteryAccessors": [{"voltage": 12.4}],
        "positionAccessors": [{"latitude": 1.5, "longitude": 100.5}],
        "doorAccessors": [{"door": n, "openMode": 0, "lock": 0} for n in (1, 2, 3, 4)],
        "windowAccessors": [{"window": 1, "openMode": 0}],
        "tyreAccessors": [{"tyre": 1, "pressure": 254.0, "temperature": 25.0,
                           "pressureFlag": 1, "temperatureFlag": 1}],
    }
    s = VehicleStatus.from_results(results, online=True, updated_ms=123)
    assert s.soc == 89.0
    assert s.range_km == 454.0 and s.odometer_km == 3205.0
    assert s.charging is True and s.charging_mode is ChargingMode.SCHEDULED
    assert s.charge_current_a is None       # 1638 placeholder -> None
    assert s.plugged_in is True and s.locked is True
    assert s.door_open is False and s.aux_voltage == 12.4
    assert s.tyres[0].pressure_kpa == 254.0 and s.tyres[0].position == "1"


def test_command_bodies():
    from gac_connect.commands import CATALOG, build_body
    assert build_body(CATALOG["status"], "V1") == {"vin": "V1"}
    ac = build_body(CATALOG["aircon-on"], "V1")
    assert ac == {"identifier": {"vin": "V1", "grade": 2, "duration": 30},
                  "operations": [{"enableAutoMode": 1, "temperature": 24.0, "temperatureUnit": 0}]}
    assert CATALOG["unlock"].pin is False and CATALOG["lock"].pin is False


def test_charge_operations():
    from zoneinfo import ZoneInfo
    from gac_connect import vehicle
    now = vehicle.charge_now_operation()
    assert now["chargingMode"] == vehicle.MODE_FREE
    win = vehicle.window_operation("23:00", "05:00", ZoneInfo("Australia/Brisbane"), weekly=127)
    assert win["dailyReservationStartTime"] == (23 * 60) * 60000


def test_session_roundtrip():
    from gac_connect.session import Session
    s = Session(token="t", refresh_token="r", expire_time=10**14, rexpire_time=10**14, region="AU")
    assert s.access_valid and s.refresh_valid
    assert Session.from_dict(s.to_dict()).token == "t"
    assert not Session(token="t", expire_time=1).access_valid


@pytest.mark.asyncio
async def test_client_constructs():
    import aiohttp
    from gac_connect.client import GacClient
    from gac_connect.errors import RegionError
    try:
        async with aiohttp.ClientSession() as http:
            c = GacClient("AU", http)
            assert c.region == "AU"
            assert c.timezone == "Australia/Brisbane"
            # IoV subsystem app id is per profile: intl regions run under "2",
            # the UK (My AION) profile under "3".
            assert c._iov_app_id == "2"
            assert GacClient("GB", http)._iov_app_id == "3"
            # IoV request AES key size is also per profile: 32 bytes (AES-256)
            # by default, 24 (AES-192) for the UK profile.
            assert c._iov_request_key_size == 32
            assert GacClient("GB", http)._iov_request_key_size == 24
            assert GacClient("GR", http).region == "GR"
            assert GacClient("IL", http).timezone == "Asia/Jerusalem"
            # a country spanning several zones can be told which one the car is in
            assert GacClient("RU", http, timezone="Asia/Vladivostok").timezone == "Asia/Vladivostok"
            with pytest.raises(RegionError):
                GacClient("ZZ", http)
            with pytest.raises(RegionError):
                GacClient("AU", http, timezone="Mars/Olympus_Mons")
            # the region default is not loaded at construction, so a system with no
            # time-zone database can still sign in and read status
            assert GacClient("AU", http)._tz is None
    except RuntimeError as exc:
        pytest.skip(str(exc))  # material missing


def test_captcha_proofs():
    from gac_connect.auth import Captcha
    # a synthetic secretKey (32B key @DS@ 16B iv) — proofs must be deterministic per x
    c = Captcha(background="", piece="", secret_key="K" * 32 + "@DS@" + "I" * 16, token="tok")
    assert c.point_json(100) == c.point_json(100)
    assert c.ticket(100) != c.point_json(100)
    assert len(c.ticket(100)) > 0


def test_status_climate_and_window():
    from gac_connect.models import VehicleStatus
    results = {
        "airConditionAccessors": [{"enableAirCompressor": 1, "windStrength": 3.0, "temperature": 21.5}],
        "steeringAccessors": [{"steering": 0}],
        "chargingAccessors": [{"dailyReservationStartTime": 82800000, "dailyReservationStopTime": 18000000,
                               "weeklyReservation": 0, "chargingMode": 1, "chargingStatus": 0}],
        "sunroofAccessors": [{"openMode": -1}],
    }
    s = VehicleStatus.from_results(results)
    assert s.ac_on is True and s.ac_target_temp_c == 21.5
    assert s.steering_heat_on is False
    assert (s.charge_window_start, s.charge_window_stop, s.charge_weekly) == ("23:00", "05:00", 0)
    assert s.sunroof_open is None   # -1 = unknown, not "closed"

    off = VehicleStatus.from_results({"airConditionAccessors": [{"enableAirCompressor": 0, "windStrength": 0.0}]})
    assert off.ac_on is False
    assert VehicleStatus.from_results({}).ac_on is None


def test_fitted_and_time_edges():
    from gac_connect.models import VehicleStatus, _hhmm
    s = VehicleStatus.from_results({
        "hatchAccessors": [{"openMode": -1, "hatch": 2}, {"openMode": 0, "hatch": 3}],
        "windowAccessors": [{"openMode": "2"}],
    })
    assert s.hatch_open is None         # a partly unknown group is not known closed
    assert s.window_open is True        # numeric strings decode
    assert VehicleStatus.from_results({"hatchAccessors": [{"openMode": "x"}]}).hatch_open is None
    assert _hhmm(0) == "00:00" and _hhmm(86_399_000) == "23:59"
    assert _hhmm(87_000_000) == "00:10"            # a stop past midnight, as the car reports it
    assert _hhmm(172_800_000) is None and _hhmm(-1) is None and _hhmm("abc") is None


def test_climate_validation():
    import pytest
    from gac_connect.commands import validate_climate
    assert validate_climate(21.3, 30) == (21.5, 30)
    assert validate_climate("24", "5") == (24.0, 5)
    for bad in ((float("nan"), 30), (float("inf"), 30), (17.9, 30), (32.1, 30), ("hot", 30),
                (22, 4), (22, 61), (22, -1), (22, "x"), (None, 30)):
        with pytest.raises(ValueError):
            validate_climate(*bad)


def test_climate_sentinels():
    from gac_connect.models import VehicleStatus
    def R(air=None, steer=None):
        groups = {"airConditionAccessors": [air] if air else None, "steeringAccessors": [steer] if steer else None}
        return VehicleStatus.from_results({k: v for k, v in groups.items() if v})
    assert R({"enableAirCompressor": 0, "windStrength": 0.0}).ac_on is False
    assert R({"enableAirCompressor": 1, "windStrength": 0.0}).ac_on is True
    assert R({"enableAirCompressor": -1, "windStrength": 2.0}).ac_on is True
    assert R({"enableAirCompressor": 0}).ac_on is None            # partial off -> unknown
    assert R({"enableAirCompressor": -1, "windStrength": -1}).ac_on is None
    assert R(steer={"steering": -1}).steering_heat_on is None
    assert R(steer={"steering": 0}).steering_heat_on is False
    assert R(steer={"steering": 1}).steering_heat_on is True


def test_climate_minutes_whole():
    import pytest
    from gac_connect.commands import validate_climate
    assert validate_climate(22, 30.0) == (22.0, 30)
    for bad in (5.9, "5.5", float("nan"), float("inf"), -float("inf"), 10**400):
        with pytest.raises(ValueError):
            validate_climate(22, bad)
    for bad_t in (float("inf"), 10**400):
        with pytest.raises(ValueError):
            validate_climate(bad_t, 30)
    from gac_connect.models import VehicleStatus
    assert VehicleStatus.from_results({"chargingAccessors": [{"weeklyReservation": float("nan")}]}).charge_weekly is None
    assert VehicleStatus.from_results({"chargingAccessors": [{"weeklyReservation": 2.5}]}).charge_weekly is None
    assert VehicleStatus.from_results({"chargingAccessors": [{"weeklyReservation": 3}]}).charge_weekly == 3


def test_horn_commands():
    from gac_connect.commands import CATALOG, build_body
    for name, tail in (("horn-on", "/horn/on"), ("horn-off", "/horn/off")):
        assert CATALOG[name].path.endswith(tail) and not CATALOG[name].pin
        assert build_body(CATALOG[name], "VIN1") == {"vin": "VIN1"}


def test_lights_state():
    from gac_connect.models import VehicleStatus
    L = lambda v: VehicleStatus.from_results({"lightAccessors": [{"open": v, "light": 1}]}).lights_on  # noqa: E731
    assert L(1) is True and L(0) is False and L(-1) is False and L(-2) is None and L("x") is None
    assert VehicleStatus.from_results({}).lights_on is None


def test_push_helpers():
    import pytest
    from gac_connect.push import BrokerInfo, interpret
    b = BrokerInfo.from_data({"host": "tcp://broker.example", "port": 2883, "clientId": "c", "username": "u",
                              "password": "p", "topics": ["/ControlResult/1"]})
    assert (b.host, b.port, b.topics, b.tls) == ("broker.example", 2883, ("/ControlResult/1",), False)
    assert BrokerInfo.from_data({"host": "ssl://b", "topics": ["t"]}).tls is True
    assert BrokerInfo.from_data({"host": "tcp://b", "port": 8883, "topics": ["t"]}).tls is False   # scheme wins
    assert BrokerInfo.from_data({"host": "b", "port": 8883, "topics": ["t"]}).tls is True            # no scheme: port
    assert BrokerInfo.from_data({"host": "b:2883", "topics": ["t"]}).port == 2883
    with pytest.raises(ValueError):
        BrokerInfo.from_data({"host": "ws://b", "topics": ["t"]})
    assert interpret(b'{"success": "false"}').ok is None
    assert interpret(b'{"success": "0"}').ok is None
    assert interpret(b'{"code": "0", "msg": "ok\\u0007\\n"}').message == "ok"
    assert len(interpret(b'{"code": 1, "msg": "' + b"x" * 500 + b'"}').message) == 120
    assert interpret(b"not json").ok is None
    assert interpret(b'{"success": true, "data": {"x": 1}}').ok is True
    assert interpret(b'{"code": "0", "msg": "SUCCESS"}').ok is True
    r = interpret(b'{"code": 13102, "msg": "refused"}')
    assert r.ok is False and r.code == "13102" and r.message == "refused"
    assert interpret(b'[1, 2]').ok is None
    r = interpret(b'{"code": 0, "msg": "success", "data": {"updateTime": 1788612254015, "identifier": {"vin": "V", "event": "control_steering", "sessionId": "s1"}, "results": []}}')
    assert r.ok is True and r.event == "control_steering" and r.session_id == "s1" and r.update_time_ms == 1788612254015
    assert r.vin == "V"
    from gac_connect import command_session_id
    assert command_session_id({"code": 13001, "data": {"sessionId": "abc"}}) == "abc"
    assert command_session_id({"code": 13001, "data": {"identifier": {"sessionId": "x"}}}) == "x"
    assert command_session_id({"code": 13001}) is None and command_session_id("nope") is None
    assert command_session_id({"data": {"identifier": "bad"}}) is None
    assert command_session_id({"data": {"identifier": {"sessionId": 7}}}) == "7"
    assert command_session_id({"data": {"sessionId": True}}) is None
    assert command_session_id({"data": {"sessionId": " s\u0007 "}}) == "s"
    assert len(command_session_id({"data": {"sessionId": "x" * 200}})) == 64
    assert interpret(b'{"code": 13001, "msg": "ongoing"}').ok is None      # interim / unknown
    assert interpret(b'{"code": 13101}').ok is False
    assert interpret(b'{"code": "weird"}').ok is None
    r = interpret(b'{"code": 0, "data": {"identifier": {"sessionId": 42, "event": ["x"], "vin": "V\\u0007"}}}')
    assert (r.session_id, r.event, r.vin) == ("42", None, "V")
    assert interpret(b'{"code": true}').code is None
    assert interpret(b'{"code": 0, "data": {"updateTime": 1.5e3}}').update_time_ms == 1500
    assert interpret(b'{"code": 0, "data": {"updateTime": NaN}}').update_time_ms is None
    assert "password" not in repr(BrokerInfo.from_data({"host": "b", "password": "secret", "topics": ["t"]}))
    assert BrokerInfo.from_data({"host": "b", "topics": "acct/1/result"}).topics == ("acct/1/result",)
    assert BrokerInfo.from_data({"host": "b", "topics": "a/1, b/2"}).topics == ("a/1", "b/2")
    assert BrokerInfo.from_data({"host": "b", "topics": ["t", 7, " ", None]}).topics == ("t",)
    assert BrokerInfo.from_data({"host": "b", "topics": {"t": 1}}).topics == ()
    assert interpret(b'{"code": null, "resultCode": "0000"}').ok is True
    assert interpret(b'{"code": null, "resultCode": null, "status": "fail"}').ok is False
    assert interpret(b'{"code": 13002}').ok is False
    assert interpret(b'{"code": 13002}').reason == "not supported by this vehicle"
    for code in (b"13003", b'"13008"', b"13903", b"15001", b"15016", b"15045", b'"11018"'):
        assert interpret(b'{"code": ' + code + b"}").ok is False
    assert interpret(b'{"code": 13007}').ok is None and interpret(b'{"code": 13007}').reason is None
    assert interpret(b'{"code": 0}').reason is None
    app = BrokerInfo.from_data({"connectHost": "ssl://b.example", "connectPort": 8884, "clientId": "c",
                                "topicList": [{"topicName": "acct/1", "topicBizCode": 1}, {"topicBizCode": 2}, "x/2"]})
    assert (app.host, app.port, app.tls, app.topics) == ("b.example", 8884, True, ("acct/1", "x/2"))
    for code in (b'"1000"', b"2000", b'"13000"'):
        assert interpret(b'{"code": ' + code + b"}").ok is True


def test_charge_power_and_time():
    from gac_connect.models import VehicleStatus
    s = VehicleStatus.from_results({"chargingAccessors": [{"chargingCurrent": 8.8, "chargingVoltage": 240.0,
                                                           "estimatedChargedDuration": 44460000}]})
    # Milliseconds in whole minutes: 44460000 is 12 h 21 min at 2.1 kW.
    assert s.charge_voltage_v == 240.0 and s.charge_power_kw == 2.11 and s.estimated_charge_minutes == 741
    s = VehicleStatus.from_results({"chargingAccessors": [{"chargingCurrent": 0.0, "chargingVoltage": 0.0,
                                                           "estimatedChargedDuration": 95}]})
    assert s.charge_power_kw is None and s.charge_voltage_v is None and s.estimated_charge_minutes == 95
    s = VehicleStatus.from_results({"chargingAccessors": [{"chargingCurrent": 0.0, "chargingVoltage": 240.0}]})
    assert s.charge_power_kw == 0.0 and s.charge_voltage_v == 240.0
    # Aion UT 2026-10-16: 24240000 while the car's screen showed 6 h 42 min.
    s = VehicleStatus.from_results({"chargingAccessors": [{"estimatedChargedDuration": 24240000,
                                                           "estimatedChargedDurationFlag": 1}]})
    assert s.estimated_charge_minutes == 404
    s = VehicleStatus.from_results({"chargingAccessors": [{"estimatedChargedDuration": 8 * 86_400_000}]})
    assert s.estimated_charge_minutes is None                       # over a week: discarded


# ---- fridge -----------------------------------------------------------------

def test_validate_fridge_defaults_and_ranges():
    from gac_connect.commands import validate_fridge
    assert validate_fridge("refrigerate") == (1, 3.0)
    assert validate_fridge("heat") == (2, 42.0)
    assert validate_fridge("freeze") == (4, -12.0)
    assert validate_fridge("refrigerate", 0) == (1, 0.0)
    assert validate_fridge("refrigerate", 20) == (1, 20.0)
    assert validate_fridge("heat", 35) == (2, 35.0)
    assert validate_fridge("freeze", -1) == (4, -1.0)
    assert validate_fridge("freeze", -15) == (4, -15.0)
    assert validate_fridge("refrigerate", 3.4) == (1, 3.0)   # whole degrees


def test_validate_fridge_rejects_bad_input():
    import pytest
    from gac_connect.commands import validate_fridge
    for mode, temp in [("off", None), ("cool", None), ("refrigerate", 21), ("refrigerate", -1),
                       ("heat", 34), ("heat", 51), ("freeze", 0), ("freeze", -16),
                       ("refrigerate", float("nan")), ("refrigerate", "warm")]:
        with pytest.raises(ValueError):
            validate_fridge(mode, temp)


def test_fridge_command_bodies():
    from gac_connect import commands
    on = commands.build_body(commands.CATALOG["fridge-on"], "VIN1",
                             {"workingMode": 4, "refrigeratorTemperature": -12.0})
    assert on == {"identifier": {"vin": "VIN1"},
                  "operations": [{"refrigeratorOperationType": "on", "workingMode": 4, "refrigeratorTemperature": -12.0}]}
    assert commands.build_body(commands.CATALOG["fridge-off"], "VIN1", {}) == {"vin": "VIN1"}


def test_status_parses_fridge():
    from gac_connect.models import VehicleStatus
    running = VehicleStatus.from_results({"refrigeratorAccessors": [
        {"leavingVehicleStatus": 0, "workingMode": 4, "surplusTime": 0.0, "refrigeratorTemperature": -12.0}]})
    assert running.fridge_fitted and running.fridge_mode == "freeze" and running.fridge_temp_c == -12.0
    off = VehicleStatus.from_results({"refrigeratorAccessors": [
        {"leavingVehicleStatus": 0, "workingMode": 3, "surplusTime": 0.0}]})
    assert off.fridge_fitted and off.fridge_mode == "off" and off.fridge_temp_c is None
    none = VehicleStatus.from_results({})
    assert not none.fridge_fitted and none.fridge_mode is None
    odd = VehicleStatus.from_results({"refrigeratorAccessors": [{"workingMode": 9}]})
    assert odd.fridge_fitted and odd.fridge_mode is None   # unknown code is not guessed


def test_validate_fridge_strict_inputs():
    import pytest
    from gac_connect.commands import validate_fridge
    for mode, temp in [("refrigerate", True), ("refrigerate", False), ([], None), (None, None), (1, None),
                       ("refrigerate", 20.4), ("freeze", -0.6), ("heat", 34.6), ("freeze", -15.4),
                       ("refrigerate", float("inf"))]:
        with pytest.raises(ValueError):
            validate_fridge(mode, temp)
    assert validate_fridge("refrigerate", 19.6) == (1, 20.0)   # in range, then rounded
    assert validate_fridge("freeze", -1.4) == (4, -1.0)


def test_fridge_on_invalid_input_never_sends():
    import asyncio
    import pytest
    from gac_connect.client import GacClient
    sent = []
    client = GacClient.__new__(GacClient)
    async def fake_command(vin, name, **kw):
        sent.append((name, kw))
    client.command = fake_command
    for kwargs in ({"mode": "off"}, {"mode": "heat", "temperature": 60}, {"mode": "refrigerate", "temperature": True}):
        with pytest.raises(ValueError):
            asyncio.run(client.fridge_on("VIN", **kwargs))
    assert sent == []
    asyncio.run(client.fridge_on("VIN", mode="heat", temperature=45))
    assert sent == [("fridge-on", {"workingMode": 2, "refrigeratorTemperature": 45.0})]


def test_status_fridge_rejects_bad_values():
    from gac_connect.models import VehicleStatus
    for code in (float("nan"), float("inf"), 1.9, True, "x", None):
        s = VehicleStatus.from_results({"refrigeratorAccessors": [{"workingMode": code, "refrigeratorTemperature": 3}]})
        assert s.fridge_mode is None, code
    for temp in (float("nan"), float("inf"), True, "cold"):
        s = VehicleStatus.from_results({"refrigeratorAccessors": [{"workingMode": 1, "refrigeratorTemperature": temp}]})
        assert s.fridge_temp_c is None, temp
    assert VehicleStatus.from_results({"refrigeratorAccessors": [{"workingMode": 1.0, "refrigeratorTemperature": "4"}]}).fridge_mode == "refrigerate"


def test_fridge_boundaries_and_rounding():
    import pytest
    from gac_connect.commands import validate_fridge
    for mode, temp in [("refrigerate", -0.4), ("heat", 50.4)]:
        with pytest.raises(ValueError):
            validate_fridge(mode, temp)
    assert validate_fridge("refrigerate", 2.5) == (1, 2.0)     # halves round to even
    assert validate_fridge("refrigerate", 3.5) == (1, 4.0)
    assert validate_fridge("heat", 35) == (2, 35.0) and validate_fridge("heat", 50) == (2, 50.0)
    assert validate_fridge("freeze", -15) == (4, -15.0) and validate_fridge("freeze", -1) == (4, -1.0)


def test_fridge_on_unhashable_mode_never_sends():
    import asyncio
    import pytest
    from gac_connect.client import GacClient
    sent = []
    client = GacClient.__new__(GacClient)
    async def fake_command(vin, name, **kw):
        sent.append(name)
    client.command = fake_command
    for mode in ([], {}, None):
        with pytest.raises(ValueError):
            asyncio.run(client.fridge_on("VIN", mode=mode))
    assert sent == []


def test_status_fridge_huge_numbers():
    from gac_connect.models import VehicleStatus
    s = VehicleStatus.from_results({"refrigeratorAccessors": [{"workingMode": 10**400, "refrigeratorTemperature": 10**400}]})
    assert s.fridge_mode is None and s.fridge_temp_c is None


def test_status_fridge_keep_running():
    from gac_connect.models import VehicleStatus
    def st(**f):
        return VehicleStatus.from_results({"refrigeratorAccessors": [{"workingMode": 1, "refrigeratorTemperature": 5, **f}]})
    assert (st(leavingVehicleStatus=1, surplusTime=45).fridge_keep_mode, st(leavingVehicleStatus=1, surplusTime=45).fridge_keep_minutes) == ("timed", 45)
    assert st(leavingVehicleStatus=1, surplusTime=30.0).fridge_keep_minutes == 30
    assert st(leavingVehicleStatus=1, surplusTime=0).fridge_keep_minutes == 0
    s = st(leavingVehicleStatus=0, surplusTime=500)            # unlimited: minutes suppressed
    assert (s.fridge_keep_mode, s.fridge_keep_minutes) == ("unlimited", None)
    for bad in ({"leavingVehicleStatus": 7}, {"leavingVehicleStatus": True}, {"leavingVehicleStatus": 1.5}, {}):
        s = st(surplusTime=30, **bad)
        assert s.fridge_keep_mode is None and s.fridge_keep_minutes is None, bad
    for bad in (-5, 2.5, float("nan"), float("inf"), float("-inf"), 10**400, True, "x", None):
        assert st(leavingVehicleStatus=1, surplusTime=bad).fridge_keep_minutes is None, bad
    none = VehicleStatus.from_results({})
    assert none.fridge_keep_mode is None and none.fridge_keep_minutes is None



class _FakeResp:
    def __init__(self, status=200, body=b'{"success": true, "data": {"results": [{}]}}', headers=None, read_error=None):
        self.status, self._body, self.headers, self._read_error = status, body, headers or {}, read_error
    async def read(self):
        if self._read_error:
            raise self._read_error
        return self._body
    async def __aenter__(self):
        return self
    async def __aexit__(self, *exc):
        return False


class _FakeHttp:
    """Counts every request that would have reached the network, and how many overlap."""
    def __init__(self, resp=None, route=None, delay=0.0):
        self.calls, self.resp, self.route, self.delay = 0, resp or _FakeResp(), route, delay
        self.kwargs, self.paths, self.in_flight, self.max_in_flight, self.times = [], [], 0, 0, []
    def post(self, url, **kw):
        import time
        self.calls += 1
        self.kwargs.append(kw)
        self.paths.append(url)
        self.times.append(time.monotonic())
        resp = self.route(url) if self.route else self.resp
        fake = self
        class _Ctx:
            async def __aenter__(self):
                import asyncio
                fake.in_flight += 1
                fake.max_in_flight = max(fake.max_in_flight, fake.in_flight)
                if fake.delay:
                    await asyncio.sleep(fake.delay)
                return resp
            async def __aexit__(self, *exc):
                fake.in_flight -= 1
                return False
        return _Ctx()


def _limiter(**kw):
    from gac_connect.limits import Limiter
    kw.setdefault("min_gap", 0.0)
    return Limiter(**kw)


def _client(http, limiter=None, token_valid=True):
    import time
    from gac_connect.client import GacClient
    c = GacClient("AU", http, limiter=limiter or _limiter())
    now = int(time.time() * 1000)
    c._session.token, c._session.expire_time = "t", now + (3_600_000 if token_valid else -1000)
    c._session.refresh_token, c._session.rexpire_time = "r", now + 3_600_000
    return c


def test_request_budget_refuses_without_sending():
    import asyncio
    import pytest
    from gac_connect.errors import RateLimitedError
    http = _FakeHttp()
    c = _client(http, _limiter(request_budgets=((60, 3), (3600, 100))))
    async def go():
        for _ in range(3):
            await c.get_status("VIN")
        for _ in range(50):                       # a looping caller
            with pytest.raises(RateLimitedError) as info:
                await c.get_status("VIN")
            assert 0 < info.value.retry_after <= 60
    asyncio.run(go())
    assert http.calls == 3


def test_retry_after_is_the_longest_wait():
    import asyncio
    import pytest
    from gac_connect.errors import RateLimitedError
    http = _FakeHttp()
    c = _client(http, _limiter(request_budgets=((60, 2), (3600, 2))))
    async def go():
        await c.get_status("VIN"); await c.get_status("VIN")
        with pytest.raises(RateLimitedError) as info:
            await c.get_status("VIN")
        assert info.value.retry_after > 3500      # the hour budget, not the minute one
    asyncio.run(go())


def test_budget_frees_up_as_the_window_moves():
    import asyncio
    http = _FakeHttp()
    lim = _limiter(request_budgets=((60, 2),))
    c = _client(http, lim)
    async def go():
        await c.get_status("VIN"); await c.get_status("VIN")
        lim._sent[0] -= 61                        # the oldest request leaves the window
        await c.get_status("VIN")
    asyncio.run(go())
    assert http.calls == 3


def test_new_clients_share_the_default_limiter():
    import asyncio
    import pytest
    from gac_connect.client import GacClient
    from gac_connect.errors import RateLimitedError
    from gac_connect.limits import DEFAULT_LIMITER
    http = _FakeHttp()
    a, b = GacClient("AU", http), GacClient("NZ", http)
    assert a._limiters == (DEFAULT_LIMITER,) and b._limiters == (DEFAULT_LIMITER,)
    own = _limiter()
    assert GacClient("AU", http, limiter=own)._limiters == (own, DEFAULT_LIMITER)
    # a shared limiter's budget binds every client using it
    lim = _limiter(request_budgets=((60, 2),))
    clients = [_client(http, lim) for _ in range(25)]
    async def go():
        for c in clients[:2]:
            await c.get_status("VIN")
        for c in clients[2:]:
            with pytest.raises(RateLimitedError):
                await c.get_status("VIN")
    asyncio.run(go())
    assert http.calls == 2


def test_requests_go_one_at_a_time_with_the_gap_after_completion():
    import asyncio
    http = _FakeHttp(delay=0.05)
    c = _client(http, _limiter(min_gap=0.1))
    async def go():
        await asyncio.gather(*(c.get_status("VIN") for _ in range(4)))
    asyncio.run(go())
    assert http.max_in_flight == 1
    starts = http.times
    assert all(b - a >= 0.15 - 0.01 for a, b in zip(starts, starts[1:], strict=False))   # 0.05 s request + 0.1 s gap


def test_command_budget_is_separate_and_counted_at_dispatch():
    import asyncio
    import pytest
    from gac_connect.errors import RateLimitedError
    http = _FakeHttp(_FakeResp(body=b'{"success": true, "data": {"results": [{}]}}'))
    lim = _limiter(command_budgets=((60, 2),))
    c = _client(http, lim)
    async def go():
        await c.command("VIN", "horn-on"); await c.command("VIN", "horn-on")
        with pytest.raises(RateLimitedError):
            await c.command("VIN", "horn-on")
        with pytest.raises(RateLimitedError):
            await c.charge_now("VIN")             # reservation commands share the budget
        await c.get_status("VIN")                 # reads are still allowed
    asyncio.run(go())
    assert http.calls == 3 and len(lim._commands) == 2   # refused commands consumed nothing


def test_a_refused_request_does_not_use_up_the_command_budget():
    import asyncio
    import pytest
    from gac_connect.errors import RateLimitedError
    http = _FakeHttp(_FakeResp(body=b'{"success": true, "data": {"results": [{}]}}'))
    lim = _limiter(request_budgets=((60, 1),), command_budgets=((60, 5),))
    c = _client(http, lim)
    async def go():
        await c.get_status("VIN")
        with pytest.raises(RateLimitedError):
            await c.command("VIN", "horn-on")
    asyncio.run(go())
    assert len(lim._commands) == 0


def test_429_pauses_everything_and_is_seen_before_the_body():
    import asyncio
    import pytest
    from gac_connect.errors import RateLimitedError
    http = _FakeHttp(_FakeResp(status=429, headers={"Retry-After": "120"}, read_error=ConnectionResetError()))
    lim = _limiter()
    c = _client(http, lim)
    async def go():
        with pytest.raises(RateLimitedError) as first:
            await c.get_status("VIN")
        assert 119 < first.value.retry_after <= 120
        http.resp = _FakeResp()
        for _ in range(10):
            with pytest.raises(RateLimitedError) as info:
                await c.get_status("VIN")
            assert 100 < info.value.retry_after <= 120
    asyncio.run(go())
    assert http.calls == 1


def test_a_waiting_request_sees_a_pause_set_while_it_waited():
    import asyncio
    from gac_connect.errors import RateLimitedError
    http = _FakeHttp(_FakeResp(status=429, headers={"Retry-After": "60"}), delay=0.05)
    c = _client(http, _limiter(min_gap=0.05))
    async def go():
        return await asyncio.gather(*(c.get_status("VIN") for _ in range(3)), return_exceptions=True)
    out = asyncio.run(go())
    assert all(isinstance(r, RateLimitedError) for r in out)
    assert http.calls == 1


def test_a_shorter_429_does_not_shorten_a_longer_pause():
    import time
    lim = _limiter()
    lim.block(3600)
    lim.block(60)
    assert lim._blocked_until - time.monotonic() > 3500


def test_retry_after_forms():
    from email.utils import format_datetime
    from datetime import UTC, datetime, timedelta
    from gac_connect.const import MAX_RATE_LIMIT_COOLDOWN, RATE_LIMIT_COOLDOWN
    from gac_connect.limits import parse_retry_after
    now = datetime.now(UTC)
    assert parse_retry_after(None) == RATE_LIMIT_COOLDOWN
    assert parse_retry_after("5") == RATE_LIMIT_COOLDOWN
    assert parse_retry_after("600") == 600
    assert parse_retry_after("7200") == 7200                      # a two-hour pause is honoured
    assert parse_retry_after("999999") == MAX_RATE_LIMIT_COOLDOWN
    assert 590 <= parse_retry_after(format_datetime(now + timedelta(minutes=10), usegmt=True)) <= 600
    assert parse_retry_after(format_datetime(now - timedelta(minutes=10), usegmt=True)) == RATE_LIMIT_COOLDOWN
    assert parse_retry_after("soon") == RATE_LIMIT_COOLDOWN


def test_redirects_are_not_followed():
    import asyncio
    import pytest
    from gac_connect.errors import ResponseError
    http = _FakeHttp(_FakeResp(status=302, headers={"Location": "https://elsewhere/"}))
    c = _client(http)
    with pytest.raises(ResponseError):
        asyncio.run(c.get_status("VIN"))
    assert http.calls == 1
    assert http.kwargs[0]["allow_redirects"] is False and http.kwargs[0]["raise_for_status"] is False


def _refresh_route(refresh_body):
    def route(url):
        if url.endswith("/refresh/token"):
            return _FakeResp(body=refresh_body)
        return _FakeResp()
    return route


def test_concurrent_callers_share_one_token_refresh():
    import asyncio
    import json
    import time
    later = int(time.time() * 1000) + 3_600_000
    body = json.dumps({"success": True, "data": {"token": "t2", "refreshToken": "r2",
                                                  "expireTime": later, "rexpireTime": later}}).encode()
    http = _FakeHttp(route=_refresh_route(body))
    c = _client(http, token_valid=False)
    async def go():
        await asyncio.gather(*(c.get_status("VIN") for _ in range(5)))
    asyncio.run(go())
    assert sum(p.endswith("/refresh/token") for p in http.paths) == 1
    assert c._session.token == "t2"


def test_a_dead_session_is_not_retried():
    import asyncio
    import json
    import pytest
    from gac_connect.errors import AuthExpiredError
    http = _FakeHttp(route=_refresh_route(json.dumps({"success": False, "code": "ACCOUNT.0015"}).encode()))
    c = _client(http, token_valid=False)
    async def go():
        for _ in range(5):
            with pytest.raises(AuthExpiredError):
                await c.get_status("VIN")
        with pytest.raises(AuthExpiredError):
            await c.command("VIN", "horn-on")
    asyncio.run(go())
    assert http.calls == 1


def test_limits_carry_across_runs_with_a_state_file(tmp_path):
    import asyncio
    import pytest
    from gac_connect.errors import RateLimitedError
    state = tmp_path / "limits.json"
    http = _FakeHttp()
    async def run_once(budget_left):
        c = _client(http, _limiter(request_budgets=((60, 2),), state_path=state))   # a fresh process
        if budget_left:
            await c.get_status("VIN")
        else:
            with pytest.raises(RateLimitedError):
                await c.get_status("VIN")
    asyncio.run(run_once(True)); asyncio.run(run_once(True)); asyncio.run(run_once(False))
    assert http.calls == 2
    lim = _limiter(state_path=tmp_path / "pause.json")
    lim.block(600)
    again = _limiter(state_path=tmp_path / "pause.json")
    again._load()
    import time
    assert again._blocked_until - time.monotonic() > 590


def test_default_budgets_allow_normal_use():
    from gac_connect.const import COMMAND_BUDGETS, MIN_REQUEST_GAP, REQUEST_BUDGETS
    budgets = dict(REQUEST_BUDGETS)
    assert budgets[3600] >= 60 + 60           # polling every minute plus a busy hour of commands and refreshes
    assert budgets[86400] >= 24 * 60 + 500    # polling every minute all day, with headroom
    assert budgets[60] <= 60 / MIN_REQUEST_GAP
    assert dict(COMMAND_BUDGETS)[60] >= 3


def test_an_own_limiter_cannot_bypass_the_shared_one():
    import asyncio
    import pytest
    from gac_connect.errors import RateLimitedError
    from gac_connect.limits import DEFAULT_LIMITER
    DEFAULT_LIMITER.request_budgets = ((60, 2),)          # restored by the conftest fixture
    http = _FakeHttp()
    async def go():
        for i in range(10):
            c = _client(http, _limiter())                     # a fresh, empty limiter every time
            if i < 2:
                await c.get_status("VIN")
            else:
                with pytest.raises(RateLimitedError):
                    await c.get_status("VIN")
    asyncio.run(go())
    assert http.calls == 2


def test_requests_are_counted_when_they_finish():
    import asyncio
    import time
    http = _FakeHttp(delay=0.2)
    lim = _limiter()
    c = _client(http, lim)
    asyncio.run(c.get_status("VIN"))
    assert time.monotonic() - lim._sent[-1] < 0.1 and lim._last_done == lim._sent[-1]


def test_threads_with_their_own_event_loops_still_go_one_at_a_time():
    import asyncio
    import threading
    lim = _limiter()
    state = {"now": 0, "max": 0}
    guard = threading.Lock()
    async def one():
        async with lim.slot():
            with guard:
                state["now"] += 1; state["max"] = max(state["max"], state["now"])
            await asyncio.sleep(0.05)
            with guard:
                state["now"] -= 1
    async def two():
        await asyncio.gather(one(), one())
    def worker():
        asyncio.run(two())
    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads: t.start()
    for t in threads: t.join(10)
    assert state["max"] == 1


def test_two_limiters_on_one_state_file_do_not_freeze_the_loop(tmp_path):
    import asyncio
    a, b = _limiter(state_path=tmp_path / "s.json"), _limiter(state_path=tmp_path / "s.json")
    order = []
    async def use(lim, name):
        async with lim.slot():
            order.append(name); await asyncio.sleep(0.05)
    async def go():
        await asyncio.wait_for(asyncio.gather(use(a, "a"), use(b, "b"), use(a, "a2")), timeout=5)
    asyncio.run(go())
    assert sorted(order) == ["a", "a2", "b"]


def test_nothing_is_sent_if_the_limits_cannot_be_recorded(tmp_path, monkeypatch):
    import asyncio
    import pathlib
    import pytest
    from gac_connect.errors import RateLimitedError
    http = _FakeHttp()
    c = _client(http, _limiter(state_path=tmp_path / "s.json"))
    def broken(self, *a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(pathlib.Path, "write_text", broken)
    with pytest.raises(RateLimitedError):
        asyncio.run(c.get_status("VIN"))
    assert http.calls == 0


def test_a_dead_session_stays_dead_even_with_an_unexpired_token():
    import asyncio
    import json
    import pytest
    from gac_connect.errors import AuthExpiredError
    def route(url):
        if url.endswith("/refresh/token"):
            return _FakeResp(body=json.dumps({"success": False, "code": "ACCOUNT.0015"}).encode())
        return _FakeResp(body=json.dumps({"success": False, "code": "SDK.GATE.0007"}).encode())
    http = _FakeHttp(route=route)
    c = _client(http)                          # token looks valid locally; the service rejects it
    async def go():
        with pytest.raises(AuthExpiredError):
            await c.get_status("VIN")              # read, refresh (spent)
        for call in (c.get_status("VIN"), c.get_position("VIN"), c.command("VIN", "horn-on"), c.mqtt_info()):
            with pytest.raises(AuthExpiredError):
                await call
    asyncio.run(go())
    assert http.calls == 2


def test_rotated_tokens_are_saved_before_use_and_shared_through_the_store():
    import asyncio
    import json
    import time
    from gac_connect.session import MemoryStore, Session
    later = int(time.time() * 1000) + 3_600_000
    body = json.dumps({"success": True, "data": {"token": "t2", "refreshToken": "r2",
                                                  "expireTime": later, "rexpireTime": later}}).encode()
    http = _FakeHttp(route=_refresh_route(body))
    expired = Session(token="t", refresh_token="r", expire_time=1, rexpire_time=later)
    store = MemoryStore(expired)
    seen = []
    orig = store.replace            # every client write is a compare-and-save
    async def replace(expected, sess):
        seen.append((sess.token, a._session.token, b._session.token))
        return await orig(expected, sess)
    store.replace = replace
    from gac_connect.client import GacClient
    a, b = GacClient("AU", http, store, limiter=_limiter()), GacClient("AU", http, store, limiter=_limiter())
    async def go():
        await a.load(); await b.load()
        await asyncio.gather(a.get_status("VIN"), b.get_status("VIN"))
    asyncio.run(go())
    assert sum(p.endswith("/refresh/token") for p in http.paths) == 1
    assert seen == [("t2", "t", "t")]           # saved while neither client used it yet
    assert a._session.token == b._session.token == "t2"


def test_an_unreadable_limits_file_pauses_instead_of_resetting(tmp_path):
    import asyncio
    import pytest
    from gac_connect.errors import RateLimitedError
    for junk in ("{broken", "null", '{"sent": "invalid"}', '{"sent": [], "commands": [], "blocked_until": 0}'):
        state = tmp_path / "s.json"
        state.write_text(junk)
        http = _FakeHttp()
        with pytest.raises(RateLimitedError):
            asyncio.run(_client(http, _limiter(state_path=state)).get_status("VIN"))
        assert http.calls == 0, junk


def test_restored_state_keeps_every_request():
    import time
    lim = _limiter()
    t = time.time() + 30          # all "in the future" (a clock that went back) collapse to now...
    assert lim.import_state({"sent": [t, t + 1, t + 2], "commands": [t, t + 1, t + 2], "blocked_until": 0, "last_done": 0})
    assert len(lim._sent) == 3 and len(lim._commands) == 3     # ...but each still counts
    assert not lim.import_state({"sent": "x"}) and len(lim._sent) == 3


def test_an_interrupted_request_is_counted_conservatively(tmp_path):
    import asyncio
    import json
    import time
    lim = _limiter(state_path=tmp_path / "s.json")
    async def go():
        async with lim.slot():
            saved = json.loads((tmp_path / "s.json").read_text())
            assert saved["sent"][-1] >= time.time() + 15      # reserved at the latest possible finish
    asyncio.run(go())
    assert json.loads((tmp_path / "s.json").read_text())["sent"][-1] <= time.time()


def test_a_dead_session_is_stored_so_other_clients_do_not_retry_it():
    import asyncio
    import json
    import time
    import pytest
    from gac_connect.client import GacClient
    from gac_connect.errors import AuthExpiredError
    from gac_connect.session import MemoryStore, Session
    later = int(time.time() * 1000) + 3_600_000
    http = _FakeHttp(route=_refresh_route(json.dumps({"success": False, "code": "ACCOUNT.0015"}).encode()))
    store = MemoryStore(Session(token="t", refresh_token="r", expire_time=1, rexpire_time=later))
    async def go():
        for _ in range(3):
            c = GacClient("AU", http, store, limiter=_limiter())
            await c.load()
            with pytest.raises(AuthExpiredError):
                await c.get_status("VIN")
    asyncio.run(go())
    assert http.calls == 1


_RUN_ONCE = r"""
import asyncio, sys, time
sys.path.insert(0, sys.argv[1])
from gac_connect.client import GacClient
from gac_connect.errors import RateLimitedError

class Resp:
    status, headers = 429, {"Retry-After": "120"}
    async def read(self): return b"{}"
    async def __aenter__(self): return self
    async def __aexit__(self, *e): return False

class Http:
    def post(self, *a, **k):
        print("SENT", flush=True)
        return Resp()

async def main():
    c = GacClient("AU", Http())
    now = int(time.time() * 1000)
    c._session.token, c._session.expire_time = "t", now + 3_600_000
    try:
        await c.get_status("VIN")
    except RateLimitedError:
        print("LIMITED", flush=True)

asyncio.run(main())
"""


def test_a_script_restarted_in_a_loop_is_still_limited(tmp_path):
    import os
    import pathlib
    import subprocess
    import sys
    src = str(pathlib.Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "GAC_CONNECT_LIMITS": str(tmp_path / "limits.json")}
    outs = [subprocess.run([sys.executable, "-c", _RUN_ONCE, src], env=env, capture_output=True, text=True, timeout=60)  # noqa: S603
            for _ in range(4)]
    sent = sum(o.stdout.count("SENT") for o in outs)
    assert sent == 1, [o.stdout + o.stderr for o in outs]   # the 429 pause holds for every later run
    assert all("LIMITED" in o.stdout for o in outs)


def test_a_corrupt_limits_file_pauses_long_then_recovers(tmp_path):
    import asyncio
    import json
    import time
    import pytest
    from gac_connect.const import RECOVERY_PAUSE
    from gac_connect.errors import RateLimitedError
    state = tmp_path / "s.json"
    state.write_text("{broken")
    http = _FakeHttp()
    with pytest.raises(RateLimitedError) as info:
        asyncio.run(_client(http, _limiter(state_path=state)).get_status("VIN"))
    assert info.value.retry_after > RECOVERY_PAUSE - 5
    assert (tmp_path / "s.corrupt").read_text() == "{broken"       # kept for inspection
    saved = json.loads(state.read_text())                           # rewritten, with the pause in it
    assert saved["blocked_until"] > time.time() + RECOVERY_PAUSE - 5
    saved["blocked_until"] = 0                                       # ...once the pause has passed
    state.write_text(json.dumps(saved))
    asyncio.run(_client(http, _limiter(state_path=state)).get_status("VIN"))
    assert http.calls == 1


def test_a_failed_recovery_write_still_pauses_later_runs(tmp_path, monkeypatch):
    import asyncio
    import pathlib
    import pytest
    from gac_connect.errors import RateLimitedError
    state = tmp_path / "s.json"
    state.write_text("{broken")
    real = pathlib.Path.write_text
    def failing(self, *a, **kw):
        if self.name.endswith(".tmp"):
            raise OSError("disk full")
        return real(self, *a, **kw)
    monkeypatch.setattr(pathlib.Path, "write_text", failing)
    http = _FakeHttp()
    with pytest.raises(RateLimitedError):
        asyncio.run(_client(http, _limiter(state_path=state)).get_status("VIN"))
    monkeypatch.setattr(pathlib.Path, "write_text", real)
    assert state.read_text() == "{broken"                 # the record was not lost
    for _ in range(5):                                     # every later run pauses again
        with pytest.raises(RateLimitedError):
            asyncio.run(_client(http, _limiter(state_path=state)).get_status("VIN"))
    assert http.calls == 0


def test_a_failed_read_keeps_the_record_and_its_pause(tmp_path, monkeypatch):
    import asyncio
    import pathlib
    import pytest
    from gac_connect.errors import RateLimitedError
    state = tmp_path / "s.json"
    lim = _limiter(state_path=state)
    lim.block(3600)
    before = state.read_text()
    real = pathlib.Path.read_text
    def flaky(self, *a, **kw):
        if self == state:
            raise OSError("temporarily unavailable")
        return real(self, *a, **kw)
    monkeypatch.setattr(pathlib.Path, "read_text", flaky)
    http = _FakeHttp()
    with pytest.raises(RateLimitedError):
        asyncio.run(_client(http, _limiter(state_path=state)).get_status("VIN"))
    monkeypatch.setattr(pathlib.Path, "read_text", real)
    assert state.read_text() == before and http.calls == 0          # untouched; the hour-long pause stands
    with pytest.raises(RateLimitedError) as info:
        asyncio.run(_client(http, _limiter(state_path=state)).get_status("VIN"))
    assert info.value.retry_after > 3500 and http.calls == 0


_RUN_OK = r"""
import asyncio, sys, time
sys.path.insert(0, sys.argv[1])
from gac_connect.client import GacClient
from gac_connect.errors import RateLimitedError
from gac_connect.limits import DEFAULT_LIMITER
DEFAULT_LIMITER.min_gap = 0.0
DEFAULT_LIMITER.request_budgets = ((3600, 3),)
DEFAULT_LIMITER.command_budgets = ((3600, 2),)

class Resp:
    status, headers = 200, {}
    async def read(self): return b'{"success": true, "data": {"results": [{}]}}'
    async def __aenter__(self): return self
    async def __aexit__(self, *e): return False

class Http:
    def post(self, *a, **k):
        print("SENT", flush=True)
        return Resp()

async def main():
    c = GacClient("AU", Http())
    now = int(time.time() * 1000)
    c._session.token, c._session.expire_time = "t", now + 3_600_000
    try:
        await (c.command("VIN", "horn-on") if sys.argv[2] == "command" else c.get_status("VIN"))
    except RateLimitedError:
        print("LIMITED", flush=True)

asyncio.run(main())
"""


def test_used_up_budgets_hold_across_separate_runs(tmp_path):
    import os
    import pathlib
    import subprocess
    import sys
    src = str(pathlib.Path(__file__).resolve().parents[1] / "src")
    def run(kind, path):
        env = {**os.environ, "GAC_CONNECT_LIMITS": str(path)}
        return subprocess.run([sys.executable, "-c", _RUN_OK, src, kind], env=env,  # noqa: S603
                              capture_output=True, text=True, timeout=60).stdout
    reads = [run("read", tmp_path / "a.json") for _ in range(5)]
    assert sum(o.count("SENT") for o in reads) == 3 and sum(o.count("LIMITED") for o in reads) == 2
    cmds = [run("command", tmp_path / "b.json") for _ in range(4)]
    assert sum(o.count("SENT") for o in cmds) == 2 and sum(o.count("LIMITED") for o in cmds) == 2


def test_clients_already_running_see_a_session_another_client_found_dead():
    import asyncio
    import json
    import time
    import pytest
    from gac_connect.client import GacClient
    from gac_connect.errors import AuthExpiredError
    from gac_connect.session import MemoryStore, Session
    later = int(time.time() * 1000) + 3_600_000
    http = _FakeHttp(route=_refresh_route(json.dumps({"success": False, "code": "ACCOUNT.0015"}).encode()))
    store = MemoryStore(Session(token="t", refresh_token="r", expire_time=1, rexpire_time=later))
    clients = [GacClient("AU", http, store, limiter=_limiter()) for _ in range(3)]
    async def go():
        for c in clients:
            await c.load()                      # all loaded before anything fails
        for c in clients:
            with pytest.raises(AuthExpiredError):
                await c.get_status("VIN")
    asyncio.run(go())
    assert http.calls == 1


def test_region_table():
    from zoneinfo import ZoneInfo

    from gac_connect.const import CONFIRMED_REGIONS, LISTED_REGIONS, REGIONS

    # Every country the 2.0.25 build configures, with the main-API host it maps to.
    assert len(REGIONS) == 56
    assert CONFIRMED_REGIONS <= LISTED_REGIONS <= set(REGIONS)
    for code, cfg in REGIONS.items():
        assert len(code) == 2 and code.isupper()
        # GAC International regions use the shared host pattern; a separate national
        # app (profile != "intl") brings its own main host.
        if cfg["profile"] == "intl":
            assert cfg["main"].endswith("-app-api.gac-international.com")
        assert cfg["iov"].endswith("-iov-sdk-access.gac-international.com")
        assert cfg["tel"].startswith("+") and cfg["tel"][1:].isdigit()
        ZoneInfo(cfg["tz"])          # raises if the zone name is wrong
    # The SE-Asia gateway only exists under its -public- name; the others do not.
    sea = {c for c, cfg in REGIONS.items() if cfg["main"].startswith("sg-")}
    assert sea and all(REGIONS[c]["iov"].startswith("sea-public-") for c in sea)
    assert not any(cfg["iov"].startswith("sea-iov") for cfg in REGIONS.values())
    # Spot checks against the app's own region switch.
    assert REGIONS["IL"]["main"] == "nl-app-api.gac-international.com"
    assert REGIONS["IL"]["iov"] == "eu-iov-sdk-access.gac-international.com"
    assert REGIONS["AE"]["iov"] == "me-iov-sdk-access.gac-international.com"
    assert REGIONS["BR"]["iov"] == "sa-iov-sdk-access.gac-international.com"
    assert REGIONS["RU"]["main"] == "ru-app-api.gac-international.com"


def test_default_timezone_is_not_loaded_until_a_charge_window_needs_it():
    """A slim image with no tz database must still build a client and read status."""
    import asyncio
    import importlib.abc
    import zoneinfo

    class _NoTzdata(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name == "tzdata" or name.startswith("tzdata."):
                raise ImportError("no tzdata (simulated minimal image)")

    from gac_connect.errors import RegionError

    async def go():
        import aiohttp
        from gac_connect.client import GacClient
        async with aiohttp.ClientSession() as http:
            c = GacClient("AU", http)          # must not raise without a zone database
            assert c.timezone == "Australia/Brisbane"
            with pytest.raises(RegionError) as exc:
                c._zone()
            assert "tzdata" in str(exc.value)

    before = zoneinfo.TZPATH
    sys.meta_path.insert(0, _NoTzdata())
    zoneinfo.reset_tzpath([])
    # ZoneInfo caches by key, so an earlier test that built this zone would hand it
    # back from the cache and hide the very thing this test checks.
    zoneinfo.ZoneInfo.clear_cache(only_keys=["Australia/Brisbane"])
    try:
        asyncio.run(go())
    except RuntimeError as exc:
        pytest.skip(str(exc))                  # material missing
    finally:
        zoneinfo.reset_tzpath(list(before))
        sys.meta_path.pop(0)
        zoneinfo.ZoneInfo.clear_cache(only_keys=["Australia/Brisbane"])


def test_a_spent_refresh_token_says_what_that_means():
    """ACCOUNT.0015 on a not-yet-expired token means the chain was advanced elsewhere."""
    import asyncio
    import json

    import pytest

    from gac_connect.errors import AuthExpiredError

    http = _FakeHttp(route=_refresh_route(json.dumps({"success": False, "code": "ACCOUNT.0015"}).encode()))
    c = _client(http, token_valid=False)

    async def go():
        with pytest.raises(AuthExpiredError) as exc:
            await c.get_status("VIN")
        message = str(exc.value)
        # Both causes, because the code alone does not distinguish them and the fix differs.
        assert "already used" in message
        assert "sign-in elsewhere" in message

    asyncio.run(go())


def test_file_store_expands_home(tmp_path, monkeypatch):
    from gac_connect.session import FileStore
    monkeypatch.setenv("HOME", str(tmp_path))
    assert FileStore("~/.config/gac-connect/session.json")._path == tmp_path / ".config/gac-connect/session.json"



def _signin_route(account_refresh_ok):
    import json
    def route(url):
        if url.endswith("/iam/sso/login/sec"):
            body = {"success": True, "data": "TICKET"}
        elif url.endswith("/gateway/sso/login"):
            body = {"success": True, "data": {"token": "v1", "refreshToken": "vr1",
                                              "expireTime": 10**14, "rexpireTime": 10**14}}
        elif url.endswith("/iam/api/user/token/refresh/sec"):
            body = ({"success": True, "code": "0000", "data": {"token": "m2", "refreshToken": "mr2"}}
                    if account_refresh_ok else {"success": False, "code": "4406"})
        else:
            raise AssertionError(f"unexpected request {url}")
        return _FakeResp(body=json.dumps(body).encode())
    return route


def test_sign_in_refreshes_the_account_token_after_minting_the_vehicle_session():
    """The vehicle session only outlives a later app sign-in once its minting account token is rotated away."""
    import asyncio
    http = _FakeHttp(route=_signin_route(account_refresh_ok=True))
    c = _client(http)
    c._session.main_token, c._session.main_refresh_token = "m1", "mr1"
    asyncio.run(c._establish_iov())
    # order matters: the rotation must come after the vehicle session exists
    assert [u.split("/gateway", 1)[-1] for u in http.paths] == [
        "/v1/iam/sso/login/sec", "/sso/login", "/v1/iam/api/user/token/refresh/sec"]
    assert (c.session.token, c.session.refresh_token) == ("v1", "vr1")
    assert (c.session.main_token, c.session.main_refresh_token) == ("m2", "mr2")


def test_a_failed_account_rotation_keeps_the_signed_in_session():
    import asyncio
    http = _FakeHttp(route=_signin_route(account_refresh_ok=False))
    c = _client(http)
    c._session.main_token, c._session.main_refresh_token = "m1", "mr1"
    asyncio.run(c._establish_iov())                      # must not raise
    assert (c.session.token, c.session.main_token) == ("v1", "m1")
    assert not c._auth_dead


def test_uk_profile_selects_its_own_backend():
    """GB (My AION UK) is a separate profile: own main host, own material, shared IoV."""
    from gac_connect.const import PROFILE_MATERIAL, REGIONS

    gb = REGIONS["GB"]
    assert gb["profile"] == "uk"
    assert gb["main"] == "app-api.aionauto.co.uk"                       # its own backend
    assert gb["iov"] == "eu-iov-sdk-access.gac-international.com"        # shared IoV gateway
    assert PROFILE_MATERIAL["uk"] == "_material_uk.pem"                 # operator-installed bundle
    # An International region is unaffected.
    assert REGIONS["AU"]["profile"] == "intl"
    assert REGIONS["AU"]["main"].endswith("gac-international.com")



def _renewal_route(*, sso_ok=True, account_ok=True, sso_fails_first=False, status_ok=True):
    """A spent vehicle refresh token, with the account session answering as configured."""
    import json
    state = {"sso": 0}
    def route(url):
        if url.endswith("/refresh/token"):
            body = {"success": False, "code": "ACCOUNT.0015"}
        elif url.endswith("/iam/sso/login/sec"):
            state["sso"] += 1
            ok = sso_ok and not (sso_fails_first and state["sso"] == 1)
            body = {"success": True, "data": "TICKET"} if ok else {"success": False, "code": "4406"}
        elif url.endswith("/gateway/sso/login"):
            body = {"success": True, "data": {"token": "v2", "refreshToken": "vr2",
                                              "expireTime": 10**14, "rexpireTime": 10**14}}
        elif url.endswith("/iam/api/user/token/refresh/sec"):
            body = ({"success": True, "code": "0000", "data": {"token": "m2", "refreshToken": "mr2"}}
                    if account_ok else {"success": False, "code": "4406"})
        else:
            return _FakeResp()
        return _FakeResp(body=json.dumps(body).encode())
    return route


def _with_account(c):
    c._session.main_token, c._session.main_refresh_token = "m1", "mr1"
    return c


def test_a_spent_vehicle_session_is_renewed_from_the_account_session():
    import asyncio
    http = _FakeHttp(route=_renewal_route())
    c = _with_account(_client(http, token_valid=False))
    asyncio.run(c.get_status("VIN"))
    assert [u.split("/gateway", 1)[-1] for u in http.paths] == [
        "/refresh/token", "/v1/iam/sso/login/sec", "/sso/login", "/v1/iam/api/user/token/refresh/sec",
        "/iov-vehicle-gateway/v1/platform/vehicle/access_gateway/get_vehicle_status"]
    assert (c.session.token, c.session.refresh_token, c.session.main_token) == ("v2", "vr2", "m2")
    assert not c._auth_dead


def test_a_refused_ticket_exchange_refreshes_the_account_token_once():
    import asyncio
    http = _FakeHttp(route=_renewal_route(sso_fails_first=True))
    c = _with_account(_client(http, token_valid=False))
    asyncio.run(c.get_status("VIN"))
    assert sum(p.endswith("/iam/sso/login/sec") for p in http.paths) == 2
    assert c.session.token == "v2" and not c._auth_dead


def test_without_a_working_account_session_a_sign_in_is_needed_once():
    import asyncio
    import pytest
    from gac_connect.errors import AuthExpiredError
    http = _FakeHttp(route=_renewal_route(sso_ok=False, account_ok=False))
    c = _with_account(_client(http, token_valid=False))
    async def go():
        for _ in range(3):
            with pytest.raises(AuthExpiredError):
                await c.get_status("VIN")
    asyncio.run(go())
    assert [u.split("/gateway", 1)[-1] for u in http.paths] == [
        "/refresh/token", "/v1/iam/sso/login/sec", "/v1/iam/api/user/token/refresh/sec"]
    assert c._auth_dead


def test_a_network_failure_while_renewing_does_not_give_up_the_session():
    import asyncio
    import pytest
    from gac_connect.errors import ResponseError
    route = _renewal_route()
    down = {"on": True}
    def flaky(url):
        if down["on"] and url.endswith("/iam/sso/login/sec"):
            return _FakeResp(status=503, body=b"unavailable")
        return route(url)
    http = _FakeHttp(route=flaky)
    c = _with_account(_client(http, token_valid=False))
    async def go():
        with pytest.raises(ResponseError):
            await c.get_status("VIN")
        assert not c._auth_dead
        down["on"] = False
        await c.get_status("VIN")
    asyncio.run(go())
    assert c.session.token == "v2"
