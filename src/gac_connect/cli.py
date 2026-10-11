"""Command-line interface: ``gac login | status | vehicles | charge | command``.

Installed with the ``[cli]`` extra. Stores its session in
``~/.config/gac-connect/session.json`` (0600). The block puzzle opens in a local
browser page during ``gac login``.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import typer
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("the CLI needs the extra: pip install 'gac-connect[cli]'") from exc

import aiohttp

from .auth import Captcha
from .client import GacClient
from .errors import CaptchaError, LoginError
from .session import FileStore

app = typer.Typer(add_completion=False, help="Unofficial GAC / Aion client.")
SESSION = Path.home() / ".config" / "gac-connect" / "session.json"

_PAGE = """<!doctype html><html lang="en"><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>GAC verification</title>
<style>body{font-family:system-ui;background:#111;color:#eee;display:flex;flex-direction:column;
align-items:center;gap:14px;padding:28px 4px;margin:0;box-sizing:border-box}.st{position:relative;width:310px;height:155px;background:#000;
border-radius:8px;overflow:hidden}.st img{position:absolute;top:0;left:0}input{width:310px}
button{font-size:16px;padding:9px 22px;border:0;border-radius:9px;background:#2e7d32;color:#fff}h1{font-size:1.2rem}#help,.m{max-width:42ch;line-height:1.5}input:focus-visible,button:focus-visible{outline:3px solid #a5d6a7;outline-offset:4px}</style>
<h1>Slide the piece into the gap</h1><div class=st><img id=b width=310 height=155 alt="Verification puzzle with a missing piece"><img id=p alt="Movable puzzle piece"></div>
<label for=s>Puzzle piece position</label>
<input id=s type=range min=0 max=263 step=1 value=0 aria-describedby=help><div id=x aria-hidden=true style=font-size:28px>0</div><div id=help>Drag the piece into the gap, then Verify. Focus the slider and use the arrow keys to move one pixel.</div>
<button id=g>Verify</button>
<div class=m id=m role=status aria-live=polite aria-atomic=true>Loading puzzle…</div><script>const s=document.getElementById('s'),p=document.getElementById('p'),
x=document.getElementById('x'),b=document.getElementById('b'),g=document.getElementById('g'),m=document.getElementById('m');
let a=0,d=0;const u=()=>{p.style.left=s.value+'px';x.textContent=s.value};s.oninput=u;
async function q(){if(d)return;const t=await(await fetch('/state')).json();if(t.a!=a){a=t.a;b.src='data:image/png;base64,'+t.b;
p.src='data:image/png;base64,'+t.p;s.value=0;u();g.disabled=false;m.textContent=a>1?'New puzzle (try '+a+')':'Puzzle ready.'}
if(t.s=='ok'){d=1;g.disabled=true;m.textContent='Verified. Back to the terminal.';return}
if(t.s=='failed'){d=1;m.textContent=t.m;return}setTimeout(q,400)}
g.onclick=async()=>{g.disabled=true;await fetch('/submit',{method:'POST',body:JSON.stringify({x:+s.value})})};q();</script></html>"""


class _Puzzle:
    def __init__(self) -> None:
        self.state = {"s": "pending", "a": 0, "b": "", "p": "", "m": ""}
        self.q: queue.Queue[int] = queue.Queue()
        self.lock = threading.Lock()
        o = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                if self.path == "/state":
                    with o.lock:
                        body = json.dumps(o.state).encode()
                else:
                    body = _PAGE.encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)
            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                o.q.put(int(json.loads(self.rfile.read(n) or b"{}").get("x", 0)))
                self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"

    def show(self, c: Captcha, a: int) -> None:
        with self.lock:
            self.state.update(s="pending", a=a, b=c.background, p=c.piece, m="")

    def done(self, ok: bool, m: str = "") -> None:
        with self.lock:
            self.state.update(s="ok" if ok else "failed", m=m)


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
            pz = _Puzzle()
            opened = False
            for attempt in range(1, 6):
                captcha = await client.start_captcha()
                pz.show(captcha, attempt)
                if not opened:
                    opened = webbrowser.open(pz.url)
                    typer.echo(f"Solve the puzzle: {pz.url}")
                x = await asyncio.to_thread(pz.q.get)
                try:
                    await client.request_sms(mobile, x)
                    pz.done(True)
                    break
                except (CaptchaError, LoginError) as exc:
                    typer.echo(f"  {exc}; new puzzle")
            else:
                raise typer.Exit(1)
            code = typer.prompt("SMS code")
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
    """Charge control: action = now. Continuous pause is unavailable."""
    if action == "pause":
        from .vehicle import PAUSE_UNSUPPORTED
        raise typer.BadParameter(PAUSE_UNSUPPORTED)
    if action != "now":
        raise typer.BadParameter("action must be now; continuous pause is unavailable")
    async def go() -> None:
        async with aiohttp.ClientSession() as http:
            client = _client(http, region, timezone or None)
            await client.load()
            await client.charge_now(vin)
            typer.echo(f"charge {action}: accepted")
    asyncio.run(go())


if __name__ == "__main__":
    app()
