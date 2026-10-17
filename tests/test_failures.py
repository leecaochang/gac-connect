import asyncio
import json
from unittest.mock import Mock, patch

import pytest
from gac_connect.errors import AuthExpiredError, GacError
from gac_connect.session import MemoryStore, Session
from gac_connect.client import GacClient


class Response:
    def __init__(self, status=200, body=None):
        self.status, self.body, self.headers = status, body, {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self):
        return json.dumps(self.body).encode()


def make_client(response, *, valid=False):
    http = Mock()
    http.post.return_value = response
    store = MemoryStore(Session(token="old", refresh_token="refresh", expire_time=10**14 if valid else 1,
                                rexpire_time=10**14))
    return GacClient("AU", http, store), http, store


@pytest.mark.parametrize("status,body", [
    (503, {"success": False, "code": 503}),
    (502, {"code": "ACCOUNT.0015"}),
    (200, {"success": False, "code": "MAINTENANCE"}),
    (200, None), (200, []), (200, {"success": True, "data": {}}),
])
def test_temporary_refresh_failure_retains_session_and_can_recover(status, body):
    client, http, store = make_client(Response(status, body))

    async def run():
        await client.load()
        before = (await store.load()).to_dict()
        with pytest.raises(GacError) as caught:
            await client._ensure_token()
        assert not isinstance(caught.value, AuthExpiredError)
        assert (await store.load()).to_dict() == before
        http.post.return_value = Response(body={"data": {"token": "new", "refreshToken": "next",
                                                       "expireTime": 10**14, "rexpireTime": 10**14}})
        await client._ensure_token()
        assert (await store.load()).refresh_token == "next"
        assert not client._auth_dead
        assert http.post.call_count == 2

    asyncio.run(run())


def test_confirmed_spent_refresh_stays_terminal():
    client, http, store = make_client(Response(body={"code": "ACCOUNT.0015"}))

    async def run():
        await client.load()
        for _ in range(2):
            with pytest.raises(AuthExpiredError):
                await client._ensure_token()
        assert (await store.load()).expired
        assert (await store.load()).refresh_token is None
        assert http.post.call_count == 1

    asyncio.run(run())


def test_cli_pause_fails_before_creating_client(monkeypatch):
    pytest.importorskip("typer")  # exercised in CI with the CLI extra installed
    from gac_connect import cli
    from typer.testing import CliRunner
    factory = Mock(side_effect=AssertionError("must not build a client"))
    monkeypatch.setattr(cli, "_client", factory)
    result = CliRunner().invoke(cli.app, ["charge", "VIN", "pause"])
    assert result.exit_code != 0
    assert "there is no pause" in result.output
    factory.assert_not_called()


@pytest.mark.parametrize("body", [None, [], True, 0, "maintenance", {"success": "false", "code": 503},
                                 {"success": 1}, {"success": False}, {}])
def test_malformed_command_response_is_not_acceptance(body):
    from gac_connect.errors import CommandError
    client, http, _ = make_client(Response(body=body), valid=True)
    async def run():
        await client.load()
        with pytest.raises(CommandError):
            await client.lock("VIN")
    asyncio.run(run())
    assert http.post.call_count == 1


@pytest.mark.parametrize("body", [{"success": True}, {"code": 13001}, {"code": "13001"}, {"code": "0000"},
                                  {"code": "0"}, {"code": 0}, {"code": "1000"}, {"code": 2000}, {"code": "13000"},
                                  {"success": False, "code": 13001}])
def test_explicit_command_acceptance_is_preserved(body):
    client, http, _ = make_client(Response(body=body), valid=True)
    async def run():
        await client.load()
        assert await client.lock("VIN") == body
    asyncio.run(run())
    assert http.post.call_count == 1


@pytest.mark.parametrize("code,expected", [(13101, "13101"), ("SDK.GATE.0017", "SDK.GATE.0017"),
                                         ("PRIVATE-TOKEN", None), (True, None), ({"token": "private"}, None)])
def test_command_rejection_exposes_only_a_bounded_service_code(code, expected):
    from gac_connect.client import _command_result
    from gac_connect.errors import CommandError

    with pytest.raises(CommandError) as caught:
        _command_result({"code": code, "msg": "private response message"})
    assert caught.value.code == expected


@pytest.mark.parametrize("method", ["lock", "unlock", "charge_now"])
def test_rejected_command_is_not_replayed_but_next_explicit_attempt_refreshes(method):
    from gac_connect.errors import TokenInvalidError
    client, http, _ = make_client(Response(), valid=True)
    http.post.side_effect = [
        Response(body={"code": "SDK.GATE.0007"}),
        Response(body={"data": {"token": "new", "refreshToken": "next", "expireTime": 10**14, "rexpireTime": 10**14}}),
        Response(body={"code": 13001}),
    ]
    async def run():
        await client.load()
        with pytest.raises(TokenInvalidError):
            await getattr(client, method)("VIN")
        assert http.post.call_count == 1
        await getattr(client, method)("VIN")
    asyncio.run(run())
    urls = [c.args[0] for c in http.post.call_args_list]
    assert len(urls) == 3
    assert urls[0] == urls[2]
    assert urls[1].endswith("/refresh/token")


@pytest.mark.parametrize("method", ["lock", "unlock"])
def test_rejection_does_not_invalidate_a_concurrently_rotated_token(method):
    from gac_connect.errors import TokenInvalidError
    client, http, _ = make_client(Response(), valid=True)
    class RotatedResponse(Response):
        async def read(self):
            client._session = Session(token="new", refresh_token="next", expire_time=10**14, rexpire_time=10**14)
            return await super().read()
    http.post.return_value = RotatedResponse(body={"code": "SDK.GATE.0007"})
    async def run():
        await client.load()
        with pytest.raises(TokenInvalidError):
            await getattr(client, method)("VIN")
        assert client.session.access_valid
        assert client.session.token == "new"
    asyncio.run(run())


@pytest.mark.parametrize("error", [TimeoutError(), __import__("aiohttp").ClientConnectionError(), GacError("outage")])
def test_optional_account_rotation_failure_keeps_successful_login(error):
    from unittest.mock import AsyncMock
    client, _http, store = make_client(Response())
    client._session.main_token = "main"
    client._session.main_refresh_token = "main-refresh"
    client._main_call = AsyncMock(side_effect=[{"success": True, "data": "ticket"}, error])
    client._iov_call = AsyncMock(return_value={"data": {"token": "new", "refreshToken": "next",
                                                       "expireTime": 10**14, "rexpireTime": 10**14}})
    async def run():
        await client._establish_iov()
        assert client.session.access_valid
        assert (await store.load()).refresh_token == "next"
        assert not client._auth_dead
    asyncio.run(run())


@pytest.mark.parametrize("field,value", [
    ("token", None), ("token", True), ("token", {"bad": "value"}), ("token", " "),
    ("refreshToken", None), ("refreshToken", ""), ("refreshToken", []),
    ("expireTime", None), ("expireTime", "tomorrow"), ("expireTime", True),
    ("expireTime", float("nan")), ("rexpireTime", None), ("rexpireTime", float("inf")),
])
def test_partial_refresh_cannot_overwrite_complete_credentials(field, value):
    data = {"token": "new", "refreshToken": "next", "expireTime": 10**14, "rexpireTime": 10**14}
    data[field] = value
    test_temporary_refresh_failure_retains_session_and_can_recover(200, {"data": data})

@pytest.mark.parametrize("error", [TimeoutError(), __import__("aiohttp").ClientConnectionError()])
def test_transport_error_preserves_credentials_and_allows_later_refresh(error):
    client, http, store = make_client(Response())
    http.post.side_effect = [
        error,
        Response(body={"data": {"token": "new", "refreshToken": "next", "expireTime": 10**14, "rexpireTime": 10**14}}),
    ]
    async def run():
        await client.load()
        before = (await store.load()).to_dict()
        with pytest.raises(GacError) as caught:
            await client._ensure_token()
        assert not isinstance(caught.value, AuthExpiredError)
        assert (await store.load()).to_dict() == before
        assert not client._auth_dead
        await client._ensure_token()
        assert (await store.load()).refresh_token == "next"
        assert http.post.call_count == 2
    asyncio.run(run())


class RawResponse(Response):
    def __init__(self, raw, status=200):
        super().__init__(status)
        self.raw = raw

    async def read(self):
        return self.raw


@pytest.mark.parametrize("response", [RawResponse(b"<html>maintenance</html>"), Response(status=302)])
def test_unreadable_replies_are_response_errors_not_command_rejections(response):
    from gac_connect.errors import CommandError, ResponseError
    client, http, _ = make_client(response, valid=True)

    async def run():
        await client.load()
        with pytest.raises(ResponseError) as caught:
            await client.get_status("VIN")
        assert not isinstance(caught.value, CommandError)
    asyncio.run(run())


@pytest.mark.parametrize("body", [{"success": False, "code": "4401"}, {"success": True}, {"success": True, "data": []},
                                  None])
def test_a_failed_vehicle_list_raises_instead_of_reporting_no_vehicles(body):
    from gac_connect.errors import ResponseError
    client, _, _ = make_client(Response(body=body), valid=True)
    with pytest.raises(ResponseError):
        asyncio.run(client.list_vehicles())


def test_vehicle_list_combines_both_lists_and_skips_records_without_a_vin():
    client, _, _ = make_client(Response(body={"success": True, "data": {
        "bindVehicles": [{"vin": "V1", "vehStyleName": "AION UT"}, {"plateNo": "NO-VIN"}, "junk"],
        "authorizedVehicles": [{"vinCode": "V2"}, {"vin": "  "}],
    }}), valid=True)
    vehicles = asyncio.run(client.list_vehicles())
    assert [v.vin for v in vehicles] == ["V1", "V2"]


def _captcha_client(*responses):
    from gac_connect.auth import Captcha
    client, http, _ = make_client(Response(), valid=True)
    http.post.side_effect = list(responses)
    client._captcha = Captcha("", "", "k" * 32 + "@DS@" + "i" * 16, "token")
    return client, http


@pytest.mark.parametrize("sms", [{"success": True}, {"success": True, "data": None},
                                 {"success": True, "data": {"verifyCodeTicket": 7}}])
def test_sms_request_with_a_malformed_success_is_a_login_error(sms):
    from gac_connect.errors import LoginError
    client, http = _captcha_client(Response(body={"success": True}), Response(body=sms))
    with pytest.raises(LoginError):
        asyncio.run(client.request_sms("0812345678", 10))
    assert client._verify_ticket is None


@pytest.mark.parametrize("login", [{"success": True}, {"success": True, "data": None},
                                   {"success": True, "data": {"refreshToken": "r"}}])
def test_code_login_without_a_token_never_starts_the_vehicle_session(login):
    from gac_connect.errors import LoginError
    client, http = _captcha_client(Response(body=login))
    client._verify_ticket = "ticket"
    with pytest.raises(LoginError):
        asyncio.run(client.login_sms("0812345678", "123456"))
    assert http.post.call_count == 1


@pytest.mark.parametrize("body", [{"success": False, "code": "0000", "data": {}}, {"code": "SDK.GATE.0017"}, None])
def test_a_failed_position_read_raises(body):
    from gac_connect.errors import ResponseError
    client, _, _ = make_client(Response(body=body), valid=True)

    async def run():
        await client.load()
        with pytest.raises(ResponseError):
            await client.get_position("VIN")
    asyncio.run(run())


@pytest.mark.parametrize("name", ["lock", "status", "horn-on"])
def test_raw_overrides_cannot_replace_the_vehicle_identity(name):
    from gac_connect.commands import CATALOG, build_body
    body = build_body(CATALOG[name], "VIN", {"vin": "OTHER", "extra": 1})
    assert body["vin"] == "VIN" and body["extra"] == 1


def test_sterilisation_sends_the_app_flat_body_and_keeps_its_vin():
    from gac_connect.commands import CATALOG, build_body
    assert build_body(CATALOG["antivirus-on"], "VIN") == {"grade": 2, "duration": 30, "vin": "VIN"}
    body = build_body(CATALOG["antivirus-on"], "VIN", {"vin": "OTHER", "duration": 10})
    assert body == {"grade": 2, "duration": 10, "vin": "VIN"}


@pytest.mark.parametrize("body", [{"success": False, "code": "0000"}, {"success": False, "code": 0}, {"code": True},
                                  {"code": 13002}, {"code": "13101"}, {"code": None}])
def test_explicit_failures_and_unknown_codes_are_rejections(body):
    from gac_connect.client import _command_result
    from gac_connect.errors import CommandError

    with pytest.raises(CommandError):
        _command_result(body)


@pytest.mark.parametrize("code,reason", [(13903, "not powered on"), ("13002", "not supported"),
                                         (15042, "not in Park"), (13007, None), ("SDK.GATE.0017", None)])
def test_known_refusal_codes_are_explained_without_the_api_message(code, reason):
    from gac_connect.client import _command_result
    from gac_connect.errors import CommandError

    with pytest.raises(CommandError) as caught:
        _command_result({"code": code, "msg": "private response message"})
    if reason:
        assert reason in caught.value.reason and reason in str(caught.value)
        assert "private response message" not in str(caught.value)
    else:
        assert caught.value.reason is None



@pytest.mark.parametrize("command,needs", [
    ("lock", "all doors are closed"), ("unlock", "not moving"), ("window-close", "switched off"),
    ("flash-on", "hazard warning lights are off"), ("aircon-manual-on", "enough range"),
    ("aircon-off", "not moving"), ("seat-1-ventilation-on", "is locked"),
    ("vehicle-power-off", "not switched on from inside"),
])
def test_conditions_not_met_names_the_app_conditions(command, needs):
    from gac_connect.client import _command_result
    from gac_connect.commands import CATALOG, refusal_hint
    from gac_connect.errors import CommandError

    assert needs in refusal_hint(13005, command) and needs in refusal_hint("13005", CATALOG[command].path)
    with pytest.raises(CommandError) as caught:
        _command_result({"code": 13005, "msg": "private response message"}, CATALOG[command].path)
    assert needs in str(caught.value) and caught.value.code == "13005"


@pytest.mark.parametrize("code,command", [(13005, "charge-now"), (13005, "sunroof-open"), (13005, "fridge-on"),
                                          (13003, "lock"), (True, "lock"), (None, "lock")])
def test_no_conditions_without_a_specific_app_list(code, command):
    from gac_connect.commands import refusal_hint

    assert refusal_hint(code, command) is None

def test_push_details_accept_the_app_connection_names():
    client, _, _ = make_client(Response(body={"code": "0000", "data": {"connectHost": "ssl://b", "topicList": ["t"]}}),
                               valid=True)

    async def run():
        await client.load()
        return await client.mqtt_info()
    assert asyncio.run(run())["connectHost"] == "ssl://b"


def test_missing_push_details_are_a_response_error():
    from gac_connect.errors import ResponseError
    client, _, _ = make_client(Response(body={"code": "0000", "data": {}}), valid=True)

    async def run():
        await client.load()
        with pytest.raises(ResponseError):
            await client.mqtt_info()
    asyncio.run(run())


@pytest.mark.parametrize("name,path,body", [
    ("antivirus-on", "control_air_condition_antivirus/on", {"grade": 2, "duration": 30, "vin": "VIN"}),
    ("antivirus-off", "control_air_condition_antivirus/off", {"vin": "VIN"}),
    ("battery-precondition-off", "battery/pretreatment",
     {"identifier": {"vin": "VIN"}, "operations": [{"batteryPretreatmentMode": 0}]}),
])
def test_app_derived_requests_use_the_app_paths(name, path, body):
    from gac_connect.capabilities import COMMAND_FEATURES, FEATURES
    from gac_connect.commands import CATALOG, build_body
    cmd = CATALOG[name]
    assert cmd.path.endswith("/access_gateway/" + path) and not cmd.pin and not cmd.stable
    assert build_body(cmd, "VIN") == body
    assert COMMAND_FEATURES[name] in FEATURES


@pytest.mark.parametrize("body,detail", [({"code": "SDK.PARAM.0001", "msg": "private"}, "result code SDK.PARAM.0001"),
                                         ({"msg": "private"}, None), (None, None)])
def test_http_errors_carry_only_a_bounded_result_code(body, detail):
    from gac_connect.errors import ResponseError
    client, _, _ = make_client(Response(status=400, body=body), valid=True)

    async def run():
        await client.load()
        with pytest.raises(ResponseError) as caught:
            await client.lock("VIN")
        return str(caught.value)
    message = asyncio.run(run())
    assert message.startswith("HTTP 400 from ") and "private" not in message
    assert (detail in message) if detail else "result code" not in message


# An Aion UT's hatches as reported: all closed, the boot opened, the bonnet opened (2026-10-14).
UT_HATCHES = {
    "closed": [{"hatch": 1, "openModeFlag": 1, "openMode": 0, "alarms": []},
               {"hatch": 3, "openModeFlag": 0, "openMode": 0, "alarms": []}],
    "boot": [{"hatch": 1, "openModeFlag": 1, "openMode": 0, "alarms": []},
             {"hatch": 3, "openModeFlag": 1, "openMode": 1, "alarms": [{"alarmObject": 3, "alarmReason": 1}]}],
    "bonnet": [{"hatch": 1, "openModeFlag": 1, "openMode": 1, "alarms": [{"alarmObject": 1, "alarmReason": 1}]},
               {"hatch": 3, "openModeFlag": 0, "openMode": 0, "alarms": []}],
}


@pytest.mark.parametrize("case,boot,bonnet", [("closed", False, False), ("boot", True, False), ("bonnet", False, True)])
def test_hatch_1_is_the_bonnet_and_hatch_3_the_boot(case, boot, bonnet):
    from gac_connect.models import VehicleStatus
    status = VehicleStatus.from_results({"hatchAccessors": UT_HATCHES[case]})
    assert (status.hatch_open, status.hood_open) == (boot, bonnet)


def test_a_flagged_off_hatch_reading_other_than_closed_is_unknown():
    from gac_connect.models import VehicleStatus
    status = VehicleStatus.from_results({"hatchAccessors": [{"hatch": 3, "openModeFlag": 0, "openMode": 1}]})
    assert status.hatch_open is None and status.hood_open is None


@pytest.mark.parametrize("brake,low", [
    ([{"alarms": []}], False),                                   # an Aion UT, as the app shows Normal
    ([{"alarms": [{"alarmReason": -1}]}], False),
    ([{"alarms": [{"alarmObject": 1, "alarmReason": 1}]}], True),
    ([{}], None),
    (None, None),
])
def test_brake_fluid_follows_the_brake_alarms_as_the_app_does(brake, low):
    from gac_connect.models import VehicleStatus
    results = {"brakeAccessors": brake} if brake is not None else {}
    assert VehicleStatus.from_results(results).brake_fluid_low is low


def test_http_error_replies_are_logged_as_a_bounded_masked_excerpt(caplog):
    import logging
    client, _, _ = make_client(Response(status=400, body={"code": "400", "msg": "startTime invalid for LGXCE4CB0SA000001",
                                                          "pad": "x" * 2000}), valid=True)
    caplog.set_level(logging.DEBUG, logger="gac_connect.client")

    async def run():
        await client.load()
        with pytest.raises(GacError):
            await client.charge_now("VIN")
    asyncio.run(run())
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("HTTP 400"))
    assert "startTime invalid for <VIN>" in line and "LGXCE4CB0SA000001" not in line
    assert len(line.split(": ", 1)[1]) == 400


def test_command_bodies_are_logged_without_the_vin(caplog):
    import logging
    from unittest.mock import AsyncMock
    client, _, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value={"code": 13001})
    caplog.set_level(logging.DEBUG, logger="gac_connect.client")

    async def run():
        await client.load()
        await client.set_charge_window("LGXCE4CB0SA000001", "22:06", "22:12", weekly=96)
    asyncio.run(run())
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("command charging/reservation"))
    assert "LGXCE4CB0SA000001" not in line and '"weeklyReservation": 96' in line and '"vin": "<VIN>"' in line


@pytest.mark.parametrize("reply", [
    {"data": {"token": "rotated", "refreshToken": "next", "expireTime": 10**14, "rexpireTime": 10**14}},
    {"code": "ACCOUNT.0015"},                                   # the old refresh token was spent
])
def test_a_sign_in_saved_during_a_refresh_is_kept(reply):
    client, http, store = make_client(Response(body=reply))
    signed_in = Session(token="fresh", refresh_token="fresh-refresh", expire_time=10**14, rexpire_time=10**14)

    async def run():
        await client.load()

        async def sign_in_meanwhile(*args, **kwargs):
            await store.save(signed_in)          # a Reconfigure finished while the refresh was out
            return Response(body=reply)
        http.post.side_effect = lambda *a, **k: _Later(sign_in_meanwhile)
        client._renew_from_account = _never_renews
        await client._ensure_token()
        saved = await store.load()
        assert (saved.token, saved.refresh_token) == ("fresh", "fresh-refresh")
        assert (client._session.token, client._auth_dead) == ("fresh", False)
    asyncio.run(run())


class _Later:
    """An aiohttp-style response whose body is produced by a coroutine when it is entered."""

    def __init__(self, make):
        self._make = make

    async def __aenter__(self):
        self._inner = await self._make()
        return await self._inner.__aenter__()

    async def __aexit__(self, *args):
        return False


async def _never_renews():
    return False


@pytest.mark.parametrize("sso", [
    {"success": False, "code": "TEMPORARY", "data": {"token": "bogus", "expireTime": "tomorrow"}},
    {"success": True, "data": {"token": "partial"}},
    {"success": True, "data": {"token": "t", "refreshToken": "r", "expireTime": "tomorrow", "rexpireTime": 10**14}},
])
def test_an_unusable_vehicle_session_reply_keeps_the_saved_session(sso):
    client, http, store = make_client(Response(body=None), valid=True)

    async def run():
        await client.load()
        client._session.main_token = "main"
        before = (await store.load()).to_dict()

        async def routed(url, *args, **kwargs):
            return Response(body={"success": True, "data": "TICKET"} if url.endswith("/iam/sso/login/sec") else sso)
        client._main_call = lambda path, body, **kw: _json(routed(path))
        client._iov_call = lambda path, body, **kw: _json(routed(path))
        with pytest.raises(GacError):
            await client._establish_iov()
        assert (await store.load()).to_dict() == before
        assert client._session.token == "old"
    asyncio.run(run())


async def _json(response):
    return (await response).body


@pytest.mark.parametrize("call", ["status", "command"])
def test_an_undecryptable_envelope_is_never_taken_as_the_answer(call):
    from gac_connect.errors import ResponseError

    client, http, _ = make_client(Response(body={"encryptKey": "bad", "encryptData": "bad", "success": True}),
                                  valid=True)

    async def run():
        await client.load()
        with pytest.raises(ResponseError, match="could not be decrypted"):
            if call == "status":
                await client.get_status("VIN")
            else:
                await client.command("VIN", "horn-on")
    asyncio.run(run())


def test_a_refused_status_read_is_not_a_report_even_with_results():
    from gac_connect.errors import ResponseError

    client, http, _ = make_client(Response(body={"success": False, "code": "13008", "data": {
        "results": [{"drivingOverviewAccessors": [{"remainElectricityPercentage": 0.81}]}]}}), valid=True)

    async def run():
        await client.load()
        with pytest.raises(ResponseError, match="13008"):
            await client.get_status("VIN")
    asyncio.run(run())


class _GatedStore(MemoryStore):
    """A store whose compare-and-save waits at a gate, so a test can act in between."""

    def __init__(self, session):
        super().__init__(session)
        self.entered, self.gate = asyncio.Event(), asyncio.Event()

    async def replace(self, expected, session):
        self.entered.set()
        await self.gate.wait()
        return await super().replace(expected, session)


ROTATED = {"data": {"token": "rotated", "refreshToken": "next", "expireTime": 10**14, "rexpireTime": 10**14}}


def test_a_refresh_cancelled_while_saving_still_keeps_the_rotated_pair():
    http = Mock()
    http.post.return_value = Response(body=ROTATED)
    store = _GatedStore(Session(token="old", refresh_token="refresh", expire_time=1, rexpire_time=10**14))
    client = GacClient("AU", http, store)

    async def run():
        await client.load()
        task = asyncio.create_task(client._ensure_token())
        await store.entered.wait()
        task.cancel()                       # e.g. the entry is unloading
        await asyncio.sleep(0)
        store.gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ((await store.load()).token, client._session.token) == ("rotated", "rotated")
    asyncio.run(run())


def test_an_unreadable_saved_session_does_not_lose_a_rotated_pair(tmp_path):
    from gac_connect.session import FileStore

    path = tmp_path / "session.json"
    store = FileStore(path)
    http = Mock()

    def rotate_and_corrupt(*args, **kwargs):
        path.write_text("{corrupt")         # unreadable by the time the rotation is saved
        return Response(body=ROTATED)
    http.post.side_effect = rotate_and_corrupt
    client = GacClient("AU", http, store)

    async def run():
        await store.save(Session(token="old", refresh_token="refresh", expire_time=1, rexpire_time=10**14))
        await client.load()
        await client._ensure_token()
        assert (await store.load()).token == "rotated"
    asyncio.run(run())


@pytest.mark.parametrize("writer", ["renewal", "account refresh"])
def test_background_writers_never_overwrite_a_newer_sign_in(writer):
    signed_in = Session(token="fresh", refresh_token="fresh-refresh", expire_time=10**14, rexpire_time=10**14)
    store = MemoryStore(Session(token="old", refresh_token="refresh", expire_time=10**14, rexpire_time=10**14,
                                main_token="main", main_refresh_token="main-refresh"))
    client = GacClient("AU", Mock(), store)

    async def run():
        await client.load()
        await store.save(signed_in)         # a Reconfigure finished meanwhile
        if writer == "renewal":
            client._main_call = _answers({"success": True, "data": "TICKET"})
            client._iov_call = _answers({"success": True, "data": {"token": "renewed", "refreshToken": "r",
                                                                   "expireTime": 10**14, "rexpireTime": 10**14}})
            await client._establish_iov()
        else:
            client._main_call = _answers({"success": True, "data": {"token": "main2", "refreshToken": "mr2"}})
            assert not await client._refresh_account_token()     # its result was not kept, so not a success
        saved = await store.load()
        assert (saved.token, client._session.token) == ("fresh", "fresh")
    asyncio.run(run())


def _answers(reply):
    async def call(*args, **kwargs):
        return reply
    return call



@pytest.mark.parametrize("ciphertext", ["", None, False, 0, [], "bad"])
def test_an_envelope_that_cannot_be_decrypted_is_an_error_whatever_it_holds(ciphertext):
    from gac_connect.errors import ResponseError

    client, http, _ = make_client(Response(body={"encryptKey": "bad", "encryptData": ciphertext, "success": True}),
                                  valid=True)

    async def run():
        await client.load()
        with pytest.raises(ResponseError, match="could not be decrypted"):
            await client.lock("VIN")
    asyncio.run(run())


def test_a_refused_refresh_never_replaces_the_credentials_even_with_tokens_in_it():
    from gac_connect.errors import ResponseError

    client, http, store = make_client(Response(body={"success": False, "code": "13008", "data": {
        "token": "t", "refreshToken": "r", "expireTime": 10**14, "rexpireTime": 10**14}}))

    async def run():
        await client.load()
        with pytest.raises(ResponseError, match="refused"):
            await client._ensure_token()
        assert ((await store.load()).token, client._auth_dead) == ("old", False)
    asyncio.run(run())


class _NewerOnReplace(MemoryStore):
    """A store where a new sign-in lands just as a background save is compared."""

    def __init__(self, session, newer):
        super().__init__(session)
        self.newer, self.entered, self.gate = newer, asyncio.Event(), asyncio.Event()

    async def replace(self, expected, session):
        await self.save(self.newer)
        self.entered.set()
        await self.gate.wait()
        return await super().replace(expected, session)


def test_a_refresh_cancelled_while_adopting_a_new_sign_in_keeps_the_new_sign_in():
    fresh = Session(token="fresh", refresh_token="fresh-refresh", expire_time=10**14, rexpire_time=10**14,
                    main_token="fresh-main", main_refresh_token="fresh-main-refresh")
    store = _NewerOnReplace(Session(token="old", refresh_token="refresh", expire_time=1, rexpire_time=10**14), fresh)
    http = Mock()
    http.post.return_value = Response(body=ROTATED)
    client = GacClient("AU", http, store)

    async def run():
        await client.load()
        task = asyncio.create_task(client._ensure_token())
        await store.entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        store.gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (client._session.token, client._base) == ("fresh", ("fresh", "fresh-refresh"))
        store.replace = MemoryStore.replace.__get__(store)          # later writers compare normally
        client._main_call = _answers({"success": True, "data": {"token": "main2", "refreshToken": "mr2"}})
        assert await client._refresh_account_token()
        saved = await store.load()
        assert (saved.token, saved.main_token) == ("fresh", "main2")    # the new sign-in survives
    asyncio.run(run())


@pytest.mark.parametrize("first", ["vehicle", "account"])
def test_overlapping_vehicle_and_account_rotations_keep_both_new_pairs(first):
    store = _GatedStore(Session(token="old", refresh_token="refresh", expire_time=1, rexpire_time=10**14,
                                main_token="old-main", main_refresh_token="old-main-refresh"))
    http = Mock()
    http.post.return_value = Response(body=ROTATED)
    client = GacClient("AU", http, store)

    async def run():
        await client.load()
        client._main_call = _answers({"success": True, "data": {"token": "fresh-main",
                                                                "refreshToken": "fresh-main-refresh"}})
        start, other = ((client._ensure_token, client._refresh_account_token) if first == "vehicle"
                        else (client._refresh_account_token, client._ensure_token))
        one = asyncio.create_task(start())
        await store.entered.wait()           # the first change is being saved
        two = asyncio.create_task(other())
        await asyncio.sleep(0.01)
        store.gate.set()
        await asyncio.gather(one, two)
        saved = await store.load()
        assert (saved.token, saved.main_token, saved.main_refresh_token) == ("rotated", "fresh-main",
                                                                              "fresh-main-refresh")
        assert client._session.token == "rotated" and client._session.main_token == "fresh-main"
    asyncio.run(run())


def test_two_file_stores_on_one_file_take_turns(tmp_path):
    import threading

    from gac_connect.session import FileStore

    path = tmp_path / "session.json"
    background, sign_in = FileStore(path), FileStore(path)
    comparing, release = threading.Event(), threading.Event()
    original = FileStore._load

    def slow_load(self):
        current = original(self)
        if self is background:
            comparing.set()
            release.wait(5)                  # compared, about to write
        return current

    async def run():
        await background.save(Session(token="old", refresh_token="refresh"))
        with patch.object(FileStore, "_load", slow_load):
            rotation = asyncio.create_task(background.replace(("old", "refresh"),
                                                              Session(token="rotated", refresh_token="next")))
            await asyncio.to_thread(comparing.wait, 5)
            login = asyncio.create_task(sign_in.save(Session(token="fresh", refresh_token="fresh-refresh")))
            await asyncio.sleep(0.05)
            release.set()
            await asyncio.gather(rotation, login)
        assert (await sign_in.load()).token == "fresh"     # the sign-in waited its turn and came last
    asyncio.run(run())


def test_the_memory_store_keeps_its_own_copy_so_renewal_rotates_the_account_token():
    store = MemoryStore(Session(token="old", refresh_token="refresh", expire_time=10**14, rexpire_time=10**14,
                                main_token="main", main_refresh_token="main-refresh"))
    client = GacClient("AU", Mock(), store)
    calls = []

    async def main_call(path, body, **kwargs):
        calls.append(path)
        if path.endswith("/iam/sso/login/sec"):
            return {"success": True, "data": "TICKET"}
        return {"success": True, "data": {"token": "main2", "refreshToken": "mr2"}}

    async def run():
        await client.load()
        client._main_call = main_call
        client._iov_call = _answers({"success": True, "data": {"token": "renewed", "refreshToken": "r",
                                                               "expireTime": 10**14, "rexpireTime": 10**14}})
        await client._establish_iov()
        assert any(p.endswith("/token/refresh/sec") for p in calls)     # the account token was rotated
        saved = await store.load()
        assert (saved.token, saved.main_token) == ("renewed", "main2")
        saved.token = "changed by a caller"
        assert (await store.load()).token == "renewed"
    asyncio.run(run())



def test_a_refused_seat_level_says_the_car_must_be_ready_with_someone_inside():
    from gac_connect.commands import refusal_hint

    hint = refusal_hint("13005", "seat-1-ventilation-on")
    assert "switched off" in hint and "press the brake" in hint and "switched on but not ready" in hint
    assert "press the brake" not in refusal_hint("13005", "lock")
