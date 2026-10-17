import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from gac_connect.push import interpret
from gac_connect.push import PushClient


@pytest.mark.parametrize("payload", [b'{"success":true}', b'[]', b'garbage', b'{{', b'null'])
def test_plaintext_cannot_bypass_configured_decryptor(payload):
    decrypt = Mock()
    client = PushClient(AsyncMock(), Mock(), decrypt=decrypt)
    assert interpret(client._unwrap(payload)).ok is None
    decrypt.assert_not_called()


@pytest.mark.parametrize("outcome", [None, ValueError("invalid envelope")])
def test_failed_decryption_does_not_accept_outer_success(outcome):
    decrypt = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
    client = PushClient(AsyncMock(), Mock(), decrypt=decrypt)
    payload = b'{"encryptData":"bad","success":true}'
    assert interpret(client._unwrap(payload)).ok is None


def test_valid_envelope_and_explicit_no_decryption_mode():
    decoded = {"success": True, "data": {"identifier": {"vin": "VIN"}}}
    client = PushClient(AsyncMock(), Mock(), decrypt=Mock(return_value=decoded))
    result = interpret(client._unwrap(b'{"encryptData":"encrypted"}'))
    assert result.ok is True and result.vin == "VIN"
    plain = PushClient(AsyncMock(), Mock())
    assert interpret(plain._unwrap(json.dumps(decoded).encode())).ok is True


@pytest.mark.parametrize("host,allow,connect", [
    ("tcp://broker.invalid", False, False), ("tcp://broker.invalid", True, True),
    ("ssl://broker.invalid", False, True),
])
def test_transport_requires_tls_or_explicit_consent(host, allow, connect):
    async def run():
        client = PushClient(AsyncMock(), Mock(), allow_plaintext=allow)
        async def fetch():
            client.stop()  # one attempt, no broker or reconnect sleeps
            return {"host": host, "topics": ["result"], "password": "synthetic"}
        client._fetch_info = fetch
        client._tls = AsyncMock(return_value=object())
        client._pump = AsyncMock()
        connection = Mock()
        connection.__aenter__ = AsyncMock(return_value=Mock(subscribe=AsyncMock()))
        connection.__aexit__ = AsyncMock(return_value=False)
        with patch("gac_connect.push.aiomqtt.Client", return_value=connection) as factory:
            await client.run()
        assert factory.called is connect
        if connect:
            assert (factory.call_args.kwargs["tls_context"] is not None) is host.startswith("ssl:")
            client._pump.assert_awaited_once()
        assert client.connected is False
        assert (client.broker.topics if client.broker else None) == (("result",) if connect else None)
        assert client.refused_plaintext is (not connect)
    asyncio.run(run())


def test_a_refused_plaintext_broker_is_not_asked_again():
    async def run():
        client = PushClient(AsyncMock(), Mock(), allow_plaintext=False)
        client._fetch_info = AsyncMock(return_value={"host": "tcp://broker.invalid", "topics": ["result"]})
        with patch("gac_connect.push.aiomqtt.Client") as factory:
            await asyncio.wait_for(client.run(), timeout=1)     # returns at once: no retry loop
        client._fetch_info.assert_awaited_once()
        factory.assert_not_called()
        assert client.refused_plaintext is True
    asyncio.run(run())


@pytest.mark.parametrize("reply,expected", [
    ({"code": 13001, "msg": "executing"}, None),
    ({"code": "13001"}, None),
    ({"code": 13005, "data": {"identifier": {"sessionId": "S1", "vin": "VIN", "event": "control_power"}}},
     (False, "13005", "S1", "VIN", "control_power")),
    ({"code": 0, "success": True, "data": {"updateTime": 1792031935000}}, (True, "0", "S1", "VIN", None)),
])
def test_a_polled_command_result_reads_like_a_push_result(reply, expected):
    from gac_connect.const import IOV_POLL_RESULT
    from test_failures import Response, make_client

    client, _, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value=reply)

    async def run():
        await client.load()
        return await client.poll_command_result("VIN", "S1")

    result = asyncio.run(run())
    client._iov_call.assert_awaited_once_with(IOV_POLL_RESULT, {"identifier": {"sessionId": "S1", "vin": "VIN"}},
                                              sensitive={"vin": "VIN"})
    if expected is None:
        assert result is None
    else:
        assert (result.ok, result.code, result.session_id, result.vin, result.event) == expected
        assert (result.reason is not None) is (result.ok is False)



def test_the_alert_list_is_one_page_from_the_account_api():
    from gac_connect.errors import ResponseError
    from test_failures import Response, make_client

    client, _, _ = make_client(Response(), valid=True)
    page = {"pageNo": 1, "list": [{"alarmId": "1", "alarmContent": "{}"}]}
    client._main_call = AsyncMock(side_effect=[{"code": "0000", "success": True, "data": page},
                                               {"code": "9999", "success": False}])

    async def run():
        await client.load()
        assert await client.get_alarms("VIN") == page
        with pytest.raises(ResponseError, match="alert list"):
            await client.get_alarms("VIN", page=2)

    asyncio.run(run())
    path, body = client._main_call.await_args_list[0].args
    assert path == "/icv/api/alarm/page/sec"
    assert body == {"pageNo": 1, "pageSize": 20, "order": "", "sort": "", "vin": "VIN"}
    assert client._main_call.await_args_list[1].args[1]["pageNo"] == 2


@pytest.mark.parametrize("version,body", [
    (None, {"vin": "VIN", "tinyId": "", "type": "oks"}),
    ("2", {"vin": "VIN", "tinyId": "", "type": "oks", "version": "2"}),
])
def test_the_departure_plan_list_matches_the_app_request(version, body):
    from test_failures import Response, make_client

    client, _, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value={"code": 0, "success": True, "data": [{"customFeatureId": "7"}]})

    async def run():
        await client.load()
        return await client.get_departure_plans("VIN", version=version)

    assert asyncio.run(run()) == [{"customFeatureId": "7"}]
    client._iov_call.assert_awaited_once_with("/iov-custom-config/new/v1/user/oks/configs/list", body,
                                              sensitive={"vin": "VIN"})



def test_an_expired_account_token_is_refreshed_once_for_the_alert_list():
    from test_failures import Response, make_client

    client, _, _ = make_client(Response(), valid=True)
    page = {"pageNo": 1, "list": []}
    client._main_call = AsyncMock(side_effect=[
        {"code": "4402", "success": False},                                       # the app signs out here
        {"code": "0000", "success": True, "data": {"token": "new", "refreshToken": "refresh2"}},
        {"code": "0000", "success": True, "data": page},
    ])
    client._persist = AsyncMock()

    async def run():
        await client.load()
        client._session.main_token, client._session.main_refresh_token = "old", "refresh"
        return await client.get_alarms("VIN")

    assert asyncio.run(run()) == page
    calls = client._main_call.await_args_list
    assert [c.args[0] for c in calls] == ["/icv/api/alarm/page/sec", "/iam/api/user/token/refresh/sec",
                                          "/icv/api/alarm/page/sec"]
    assert calls[2].kwargs["authorization"] == "new"
    assert client.last_account_refresh[1] is True


def test_a_refused_refresh_ends_the_alert_list_and_is_recorded():
    from test_failures import Response, make_client
    from gac_connect.errors import AccountSessionEnded

    client, _, _ = make_client(Response(), valid=True)
    client._main_call = AsyncMock(side_effect=[{"code": "4402", "success": False},
                                               {"code": "4421", "success": False}])

    async def run():
        await client.load()
        client._session.main_token, client._session.main_refresh_token = "old", "refresh"
        await client.get_alarms("VIN")

    with pytest.raises(AccountSessionEnded):
        asyncio.run(run())
    assert client.last_account_refresh[1] is False


def test_signed_in_on_another_phone_ends_the_alert_list_without_a_refresh():
    from test_failures import Response, make_client
    from gac_connect.errors import AccountSessionEnded

    client, _, _ = make_client(Response(), valid=True)
    client._main_call = AsyncMock(return_value={"code": "4423", "success": False})

    async def run():
        await client.load()
        client._session.main_token, client._session.main_refresh_token = "old", "refresh"
        await client.get_alarms("VIN")

    with pytest.raises(AccountSessionEnded, match="signed in on another phone"):
        asyncio.run(run())
    client._main_call.assert_awaited_once()                # the app's session is left alone
    assert client.last_account_refresh is None



@pytest.mark.parametrize("default,path", [(False, "/iov-custom-config/v1/user/oks/config/query"),
                                          (True, "/iov-custom-config/v1/default/config/query")])
def test_the_older_standby_setting_is_read_like_the_app(default, path):
    from test_failures import Response, make_client

    client, _, _ = make_client(Response(), valid=True)
    client._iov_call = AsyncMock(return_value={"code": 0, "success": True, "data": {"active": 1}})

    async def run():
        await client.load()
        return await client.get_standby_config("VIN", default=default)

    assert asyncio.run(run()) == {"active": 1}
    client._iov_call.assert_awaited_once_with(path, {"vin": "VIN", "type": "oks"}, sensitive={"vin": "VIN"})


def test_a_refusal_names_what_the_car_reported():
    from gac_connect.push import interpret

    # Lock with the driver's door open, polled (Aion UT, 2026-10-16; VIN removed).
    reply = {"msg": "任务执行条件不满足", "code": 13005, "data": {
        "identifier": {"sessionId": "2_s", "vin": "VIN", "event": "control_door"},
        "results": [
            {"door": 1, "lock": 1, "openMode": 0, "itemInfos": [{"operateName": "lock", "itemErrorCode": 13005,
             "additionInfos": [{"additionValue": "1.0", "additionDes": "Driver door ajar status", "additionNo": 12},
                               {"additionValue": "0.0", "additionDes": "BCM leave out remote mode reasons",
                                "additionNo": 43}]}]},
            {"door": 2, "lock": 1, "openMode": 1}]}}
    result = interpret(reply)
    assert result.ok is False and result.code == "13005"
    assert result.details == ("Driver door ajar",)
    reply["data"]["results"][0]["itemInfos"][0]["additionInfos"][1]["additionValue"] = "3.0"
    assert interpret(reply).details == ("Driver door ajar", "BCM leave out remote mode reasons: 3")
    # An accepted command's additionInfos (vehicle speed and the like) are not causes.
    reply.update(code=0, msg="success")
    reply["data"]["results"][0]["itemInfos"][0]["itemErrorCode"] = 0
    assert interpret(reply).details == ()
