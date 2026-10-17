"""The shared slide-puzzle session: wrong slides, refused sign-ins, limits and the page."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gac_connect.errors import CaptchaError, GacError, LoginError, RateLimitedError
from gac_connect.puzzle import PuzzleSession, offset, page


def puzzle(n):
    return SimpleNamespace(background=f"bg{n}", piece=f"piece{n}")


def session(**sms):
    client = SimpleNamespace(request_sms=AsyncMock(**sms), start_captcha=AsyncMock(return_value=puzzle(2)))
    return PuzzleSession(client, "0812345678", puzzle(1)), client


def test_a_right_slide_sends_the_sms_once():
    s, client = session()
    assert asyncio.run(s.submit(120)) == {"ok": True, "verified": True}
    client.request_sms.assert_awaited_once_with("0812345678", 120)
    assert asyncio.run(s.submit(120)) == {"ok": True}          # nothing more is sent
    assert client.request_sms.await_count == 1 and s.state()["status"] == "ok"


def test_a_wrong_slide_shows_a_new_puzzle():
    s, client = session(side_effect=CaptchaError("rejected"))
    assert asyncio.run(s.submit(5)) == {"ok": False, "retry": True}
    state = s.state()
    assert (state["attempt"], state["bg"], state["status"]) == (2, "bg2", "pending")


def test_a_wrong_slide_without_a_new_puzzle_fetches_one_before_sending_again():
    s, client = session(side_effect=CaptchaError("rejected"))
    client.start_captcha.side_effect = [GacError("offline"), puzzle(3)]
    assert asyncio.run(s.submit(5)) == {"ok": False}             # could not fetch: try again
    client.request_sms.reset_mock()
    assert asyncio.run(s.submit(5)) == {"ok": False, "retry": True}
    client.request_sms.assert_not_awaited()                      # the new puzzle is solved first
    assert s.state()["bg"] == "bg3"


def test_a_refused_sign_in_stops_with_its_reason():
    s, _ = session(side_effect=LoginError("number not registered"))
    assert asyncio.run(s.submit(5)) == {"ok": False, "failed": True}
    assert (s.state()["status"], s.state()["msg"]) == ("failed", "number not registered")
    assert asyncio.run(s.submit(5)) == {"ok": False, "failed": True}


def test_limits_and_network_errors_ask_to_try_again():
    s, _ = session(side_effect=RateLimitedError("slow down", retry_after=30))
    assert asyncio.run(s.submit(5)) == {"ok": False, "wait": 30}
    state = s.state()
    assert state["nid"] == 1 and "Wait 30 s" in state["notice"] and 28 <= state["wait"] <= 30
    s, _ = session(side_effect=TimeoutError())
    assert asyncio.run(s.submit(5)) == {"ok": False}
    assert "Could not reach GAC" in s.state()["notice"] and s.status == "pending"


def test_one_slide_is_checked_at_a_time():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(*args):
            started.set()
            await release.wait()
        s, _ = session(side_effect=slow)
        first = asyncio.create_task(s.submit(5))
        await started.wait()
        assert await s.submit(6) == {"ok": False, "busy": True}
        release.set()
        assert await first == {"ok": True, "verified": True}
    asyncio.run(run())


@pytest.mark.parametrize("value", [-1, 264, True, "10", None, 1.5])
def test_only_slider_positions_are_accepted(value):
    with pytest.raises(ValueError):
        offset(value)
    assert offset(0) == 0 and offset(263) == 263


def test_the_page_names_its_host():
    assert "Start again from Home Assistant." in page("Home Assistant")
    with pytest.raises(ValueError):
        page("<script>")


def test_the_cli_serves_the_shared_page_and_hands_each_verify_to_the_session(monkeypatch):
    import json
    import sys
    import urllib.error
    import urllib.request

    if "typer" not in sys.modules:     # the CLI extra may be absent; only its decorator is needed here
        fake = SimpleNamespace(Typer=lambda **kw: SimpleNamespace(command=lambda *a, **k: (lambda f: f)),
                               Option=lambda *a, **k: None, echo=print, prompt=input, Exit=SystemExit)
        monkeypatch.setitem(sys.modules, "typer", fake)
    from gac_connect.cli import _PuzzlePage

    async def run():
        s, client = session()
        pz = _PuzzlePage(s, asyncio.get_running_loop())

        def fetch(path, body=None):
            request = urllib.request.Request(pz.url + path, data=body, method="POST" if body else "GET")  # noqa: S310
            try:
                with urllib.request.urlopen(request, timeout=5) as reply:  # noqa: S310 (127.0.0.1 only)
                    return reply.status, reply.read().decode()
            except urllib.error.HTTPError as err:
                return err.code, err.read().decode()
        try:
            status, html = await asyncio.to_thread(fetch, "")
            assert status == 200 and "return to the terminal" in html
            status, body = await asyncio.to_thread(fetch, "?state=1")
            assert json.loads(body)["attempt"] == 1
            assert (await asyncio.to_thread(fetch, "?", b'{"x": 999}'))[0] == 400
            status, body = await asyncio.to_thread(fetch, "?", b'{"x": 120}')
            assert status == 200 and json.loads(body)["verified"] is True
            client.request_sms.assert_awaited_once_with("0812345678", 120)
        finally:
            pz.httpd.shutdown()
    asyncio.run(run())
