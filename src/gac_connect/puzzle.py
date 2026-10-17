"""The slide-puzzle step of signing in, shared by every front end (the CLI, Home Assistant).

``PuzzleSession`` holds the puzzle on show and turns each Verify into ``request_sms``: a
wrong slide fetches a new puzzle, a refused sign-in stops, and a rate limit or a network
error asks the user to try again. ``page(host)`` is the browser page: it polls its own
URL with ``state=1`` added for ``PuzzleSession.state()`` and POSTs ``{"x": offset}`` to
its own URL; the front end only serves the two. Only the user solves the puzzle; nothing
here measures the gap.
"""
from __future__ import annotations

import re
import time
from typing import Any

import aiohttp

from .auth import Captcha
from .errors import CaptchaError, GacError, LoginError, RateLimitedError

MAX_OFFSET = 263       # the slider's range, in puzzle pixels

PAGE = """<!doctype html><html lang="en"><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>GAC verification</title>
<style>body{font-family:system-ui,-apple-system,sans-serif;background:#111;color:#eee;display:flex;
flex-direction:column;align-items:center;gap:16px;padding:28px 4px;margin:0;box-sizing:border-box}.st{position:relative;width:310px;height:155px;
background:#000;border-radius:8px;overflow:hidden}.st img{position:absolute;top:0;left:0}input{width:310px}
button{font-size:16px;font-weight:700;padding:10px 24px;border:0;border-radius:9px;background:#2e7d32;color:#fff}
button:disabled{background:#444}.m{min-height:1.3em}.ok{color:#4ade80;font-weight:700}.bad{color:#f87171}
h1{font-size:1.2rem}#help,.m{max-width:42ch;line-height:1.5}input:focus-visible,button:focus-visible{outline:3px solid #a5d6a7;outline-offset:4px}</style>
<h1>Slide the piece into the gap</h1>
<div class=st><img id=b width=310 height=155 alt="Verification puzzle with a missing piece"><img id=p alt="Movable puzzle piece"></div>
<label for=s>Puzzle piece position</label>
<input id=s type=range min=0 max=263 step=1 value=0 aria-describedby=help>
<div id=help>Drag the piece into the gap, then Verify. Focus the slider and use the arrow keys to move one pixel.</div>
<button id=g>Verify</button><div class=m id=m role=status aria-live=polite aria-atomic=true>Loading puzzle…</div>
<script>
const Q=new URLSearchParams(location.search);Q.delete('state');const P='?'+Q,S=P+(Q.toString()?'&':'')+'state=1';
const s=document.getElementById('s'),p=document.getElementById('p'),
b=document.getElementById('b'),g=document.getElementById('g'),m=document.getElementById('m');
let at=0,nid=0,done=false;const u=()=>{p.style.left=s.value+'px'};s.oninput=u;
const verified=()=>{done=true;g.disabled=true;m.textContent='Verified ✓ — return to __HOST__.'};
async function poll(){if(done)return;let t;try{const r=await fetch(S,{headers:{accept:'application/json'}});
if(done)return;
if(r.status==404){done=true;g.disabled=true;m.textContent='This sign-in has expired. Start again from __HOST__.';return}
if(!r.ok)throw new Error(r.status);t=await r.json()}
catch(e){setTimeout(poll,2000);return}
if(done)return;
if(t.attempt!=at){at=t.attempt;b.src='data:image/png;base64,'+t.bg;p.src='data:image/png;base64,'+t.piece;s.value=0;u();g.disabled=false;m.textContent=at>1?'Not quite — new puzzle (try '+at+').':'Puzzle ready.'}
if(t.status=='ok'){verified();return}
if(t.status=='failed'){done=true;g.disabled=true;m.textContent=t.msg;return}
if(t.nid!=nid){nid=t.nid;m.innerHTML='';const sp=document.createElement('span');sp.className='bad';sp.textContent=t.notice;m.appendChild(sp);
setTimeout(()=>{if(!done)g.disabled=false},Math.max(0,t.wait)*1000)}
setTimeout(poll,500)}
g.onclick=async()=>{g.disabled=true;m.textContent='Checking…';
const fail=t=>{m.textContent=t;setTimeout(()=>{if(!done)g.disabled=false},3000)};
try{const r=await fetch(P,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({x:+s.value})});
if(r.status==404){done=true;m.textContent='This sign-in has expired. Start again from __HOST__.'}
else if(r.ok){const result=await r.json();if(result.ok)verified()}
else if(r.status!=409)fail('Something went wrong. Press Verify to try again.')}
catch(e){fail('Could not reach __HOST__. Try again in a moment.')}};
poll();</script></html>"""


def page(host: str) -> str:
    """The puzzle page, naming ``host`` (e.g. "Home Assistant") in its messages."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z ]{0,39}", host):
        raise ValueError("host must be a short plain name")
    return PAGE.replace("__HOST__", host)


def offset(value: Any) -> int:
    """A Verify's slider position, or ValueError."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_OFFSET:
        raise ValueError("invalid puzzle offset")
    return value


class PuzzleSession:
    """One sign-in's puzzle, from the first puzzle (fetched with ``start_captcha``) to the SMS."""

    def __init__(self, client: Any, mobile: str, captcha: Captcha) -> None:
        self._client, self._mobile = client, mobile
        self.captcha = captcha
        self.attempt = 1
        self.status = "pending"        # pending, ok (the SMS was sent) or failed
        self.message = ""
        self.busy = False
        self._need_new = False
        self._notice, self._notice_until, self._nid = "", 0.0, 0

    async def submit(self, x: int) -> dict[str, Any]:
        """Check a slide; ``{"ok": True, "verified": True}`` once the SMS has been sent.

        Otherwise ``ok`` is False with ``retry`` (a new puzzle is on show), ``failed``
        (sign-in was refused; ``message`` says why), ``wait`` (seconds before trying
        again) or ``busy`` (a slide is already being checked).
        """
        if self.status == "ok":
            return {"ok": True}
        if self.status == "failed":
            return {"ok": False, "failed": True}
        if self.busy:
            return {"ok": False, "busy": True}
        self.busy = True
        try:
            if self._need_new:
                # the last slide was wrong but no new puzzle could be fetched: show one now;
                # the user solves it before anything else is sent
                await self._new_puzzle()
                return {"ok": False, "retry": True}
            try:
                await self._client.request_sms(self._mobile, offset(x))
            except CaptchaError:
                self._need_new = True        # wrong slide (or a bad ticket): a fresh puzzle
                await self._new_puzzle()
                return {"ok": False, "retry": True}
            except LoginError as err:
                # The slide was accepted but sign-in was refused (for example a number not
                # registered in this region); a new puzzle cannot help.
                self.status, self.message = "failed", str(err)
                return {"ok": False, "failed": True}
        except RateLimitedError as err:
            wait = int(err.retry_after or 60)
            self._show_notice(f"Too many requests to GAC just now. Wait {wait} s, then press Verify again.", wait)
            return {"ok": False, "wait": wait}
        except (GacError, aiohttp.ClientError, TimeoutError):
            self._show_notice("Could not reach GAC. Press Verify to try again.", 3)
            return {"ok": False}
        finally:
            self.busy = False
        self.status = "ok"
        return {"ok": True, "verified": True}

    def state(self) -> dict[str, Any]:
        """What the page polls for."""
        return {
            "nid": self._nid,
            "notice": self._notice,
            "wait": max(0, round(self._notice_until - time.monotonic())),
            "attempt": self.attempt,
            "status": self.status,
            "msg": self.message,
            "bg": self.captcha.background,
            "piece": self.captcha.piece,
        }

    async def _new_puzzle(self) -> None:
        self.captcha = await self._client.start_captcha()
        self._need_new = False
        self.attempt += 1

    def _show_notice(self, text: str, wait: int) -> None:
        """A message for the page; Verify re-enables once ``wait`` seconds pass."""
        self._notice, self._notice_until = text, time.monotonic() + wait
        self._nid += 1
