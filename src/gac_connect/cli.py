"""Command-line interface: ``gac login | status | vehicles | charge | command``.

Installed with the ``[cli]`` extra. Stores its session in
``~/.config/gac-connect/session.json`` (0600). The block puzzle opens in a local
browser page during ``gac login``.
"""
from __future__ import annotations

import asyncio
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import typer
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("the CLI needs the extra: pip install 'gac-connect[cli]'") from exc

import aiohttp

from . import puzzle
from .client import GacClient
from .puzzle import PuzzleSession
from .session import FileStore

app = typer.Typer(add_completion=False, help="Unofficial GAC / Aion client.")
SESSION = Path.home() / ".config" / "gac-connect" / "session.json"

class _PuzzlePage:
    """Serves the shared puzzle page on 127.0.0.1 and hands each Verify to the session."""

    def __init__(self, session: PuzzleSession, loop: asyncio.AbstractEventLoop) -> None:
        html = puzzle.page("the terminal").encode()

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _reply(self, status: int, body: bytes, kind: str) -> None:
                self.send_response(status)
                self.send_header("content-type", kind)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if "state=1" in self.path:
                    state = session.state()
                    self._reply(200, json.dumps(state).encode(), "application/json")
                else:
                    self._reply(200, html, "text/html; charset=utf-8")

            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                try:
                    data = json.loads(self.rfile.read(n) or b"{}")
                    x = puzzle.offset(data.get("x") if isinstance(data, dict) else None)
                except ValueError:
                    self._reply(400, b'{"ok": false}', "application/json")
                    return
                result = asyncio.run_coroutine_threadsafe(session.submit(x), loop).result()
                self._reply(409 if result.get("busy") else 200, json.dumps(result).encode(), "application/json")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"


async def _run(coro):
    return await coro


def _client(http: aiohttp.ClientSession, region: str, timezone: str | None = None) -> GacClient:
    # the shared limiter keeps its state between runs, so a loop around the CLI is limited too
    return GacClient(region, http, FileStore(SESSION), timezone=timezone)


@app.command()
def login(mobile: str = typer.Option(..., prompt=True), region: str = "AU") -> None:
    """Sign in with your mobile number and an SMS code."""
    async def go() -> None:
        async with aiohttp.ClientSession() as http:
            client = _client(http, region)
            session = PuzzleSession(client, mobile, await client.start_captcha())
            pz = _PuzzlePage(session, asyncio.get_running_loop())
            webbrowser.open(pz.url)
            typer.echo(f"Solve the puzzle: {pz.url}")
            while session.status == "pending":
                await asyncio.sleep(0.3)
            pz.httpd.shutdown()
            if session.status == "failed":
                typer.echo(session.message)
                raise typer.Exit(1)
            code = await asyncio.to_thread(typer.prompt, "SMS code")
            await client.login_sms(mobile, code)
            typer.echo("Signed in.")
    asyncio.run(go())


@app.command()
def vehicles(region: str = "AU") -> None:
    """List the vehicles on the account."""
    async def go() -> None:
        async with aiohttp.ClientSession() as http:
            client = _client(http, region)
            await client.load()
            for v in await client.list_vehicles():
                typer.echo(f"{v.vin}  {v.model or ''}  {v.plate or ''}")
    asyncio.run(go())


@app.command()
def status(vin: str, region: str = "AU") -> None:
    """Show live status for a VIN."""
    async def go() -> None:
        async with aiohttp.ClientSession() as http:
            client = _client(http, region)
            await client.load()
            s = await client.get_status(vin)
            typer.echo(json.dumps({
                "soc": s.soc, "range_km": s.range_km, "odometer_km": s.odometer_km,
                "charging": s.charging, "plugged_in": s.plugged_in, "locked": s.locked,
                "cabin_temp_c": s.cabin_temp_c, "aux_voltage": s.aux_voltage,
                "tyres_kpa": [t.pressure_kpa for t in s.tyres],
            }, indent=2))
    asyncio.run(go())


@app.command()
def charge(vin: str, action: str, region: str = "AU", timezone: str = "") -> None:
    """Charge control: action = now. There is no pause; a charge window blocks AC charging outside it."""
    if action != "now":
        raise typer.BadParameter("action must be now; there is no pause, set a charge window instead")
    async def go() -> None:
        async with aiohttp.ClientSession() as http:
            client = _client(http, region, timezone or None)
            await client.load()
            await client.charge_now(vin)
            typer.echo(f"charge {action}: accepted")
    asyncio.run(go())


if __name__ == "__main__":
    app()
