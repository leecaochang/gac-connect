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
