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


@pytest.mark.parametrize("body", [{"success": True}, {"code": 13001}, {"code": "13001"}, {"code": "0000"}])
def test_explicit_command_acceptance_is_preserved(body):
    client, http, _ = make_client(Response(body=body), valid=True)
    async def run():
        await client.load()
        assert await client.lock("VIN") == body
    asyncio.run(run())
    assert http.post.call_count == 1


@pytest.mark.parametrize("method", ["lock", "charge_now"])
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


def test_rejection_does_not_invalidate_a_concurrently_rotated_token():
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
            await client.lock("VIN")
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
