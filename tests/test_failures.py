import asyncio
import json
from unittest.mock import Mock

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


def test_pause_never_refreshes_or_sends():
    from gac_connect.errors import CommandError
    client, http, _store = make_client(Response())
    with pytest.raises(CommandError, match="Continuous charging pause"):
        asyncio.run(client.charge_pause("VIN"))
    http.post.assert_not_called()


def test_cli_pause_fails_before_creating_client(monkeypatch):
    pytest.importorskip("typer")  # exercised in CI with the CLI extra installed
    from gac_connect import cli
    from typer.testing import CliRunner
    factory = Mock(side_effect=AssertionError("must not build a client"))
    monkeypatch.setattr(cli, "_client", factory)
    result = CliRunner().invoke(cli.app, ["charge", "VIN", "pause"])
    assert result.exit_code != 0
    assert "Continuous charging pause" in result.output
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


def test_anti_bodies_keep_their_identifier():
    from gac_connect.commands import Command, build_body
    body = build_body(Command("x", "/x", "anti"), "VIN", {"identifier": {"vin": "OTHER"}, "currentDuration": 10})
    assert body == {"identifier": {"vin": "VIN"}, "currentDuration": 10}


@pytest.mark.parametrize("body", [{"success": False, "code": "0000"}, {"success": False, "code": 0}, {"code": True},
                                  {"code": 13002}, {"code": "13101"}, {"code": None}])
def test_explicit_failures_and_unknown_codes_are_rejections(body):
    from gac_connect.client import _command_result
    from gac_connect.errors import CommandError

    with pytest.raises(CommandError):
        _command_result(body)
