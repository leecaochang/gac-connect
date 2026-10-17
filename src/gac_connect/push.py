"""Push channel: the service's MQTT feed of command results.

Every remote command is accepted asynchronously; the outcome (applied by the
car, or refused) is published on a per-account topic. :class:`PushClient`
fetches the broker details, keeps a subscription open and hands each message to
a callback. Callbacks get the topic, the plain payload and an :func:`interpret` of it
(outcome, the command event it answers, session id).

The broker credentials are short-lived, so they are fetched again on every
(re)connect. Reconnects back off from 5 s to 5 min.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

try:
    import aiomqtt
except ImportError:  # pragma: no cover
    aiomqtt = None

from .const import SUCCESS_RESULT_CODES
from .errors import REFUSAL_CODES, GacError, result_reason

_LOGGER = logging.getLogger(__name__)

MessageHandler = Callable[[str, bytes, "PushResult"], Awaitable[None] | None]

STABLE_SECONDS = 60          # a connection this old resets the reconnect backoff

# Result codes with a known meaning; anything else is reported as unknown.
SUCCESS_CODES = SUCCESS_RESULT_CODES | {"200", "success"}
FAILURE_CODES = REFUSAL_CODES | {"fail", "failed", "error"}


def _topics(value: Any) -> tuple[str, ...]:
    """Topic names from a list (of names or ``{"topicName": …}`` records), or one comma-separated string."""
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)):
        return ()
    names = (item.get("topicName") if isinstance(item, dict) else item for item in value)
    return tuple(name.strip() for name in names if isinstance(name, str) and name.strip())


@dataclass(frozen=True)
class BrokerInfo:
    host: str
    port: int
    client_id: str
    username: str
    password: str = field(repr=False)
    topics: tuple[str, ...] = ()
    tls: bool = False

    @classmethod
    def from_data(cls, data: dict) -> BrokerInfo:
        """Read broker details. An explicit scheme decides the transport; without
        one, only the standard TLS port 8883 implies TLS. The app's alternative
        names (connectHost, connectPort, topicList) are accepted too."""
        data = {**data, "host": data.get("host") or data.get("connectHost"),
                "port": data.get("port") or data.get("connectPort"),
                "topics": data.get("topics") or data.get("topicList")}
        host = str(data.get("host") or "")
        tls: bool | None = None
        schemes = (("tcp://", False), ("mqtt://", False), ("ssl://", True), ("mqtts://", True), ("tls://", True))
        for prefix, secure in schemes:
            if host.startswith(prefix):
                host, tls = host[len(prefix):], secure
                break
        if "://" in host:
            raise ValueError(f"unsupported broker scheme in {host!r}")
        if ":" in host and not data.get("port"):
            host, _, port_s = host.rpartition(":")
            data = {**data, "port": port_s}
        if tls is None:
            tls = int(data.get("port") or 0) == 8883
        port = int(data.get("port") or (8883 if tls else 1883))
        return cls(
            host=host, port=port, tls=tls,
            client_id=str(data.get("clientId") or ""), username=str(data.get("username") or ""),
            password=str(data.get("password") or ""), topics=_topics(data.get("topics")),
        )


@dataclass(frozen=True)
class PushResult:
    """Reading of a result message."""
    ok: bool | None          # True = applied, False = refused/failed, None = unknown
    code: str | None
    message: str | None
    data: Any
    event: str | None = None        # which command this answers, e.g. "control_steering"
    session_id: str | None = None
    vin: str | None = None
    update_time_ms: int | None = None
    reason: str | None = None       # this library's explanation of a known refusal code
    details: tuple[str, ...] = ()   # what the car itself names as the cause of a refusal


def _clean(value: Any, limit: int = 120) -> str | None:
    """Printable, bounded text for a service-provided message; None otherwise."""
    if not isinstance(value, str):
        return None
    text = "".join(ch for ch in value if ch.isprintable()).strip()
    return text[:limit] or None


def _millis(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


def normalize_id(value: Any, limit: int = 64) -> str | None:
    """A code / event / id as a short printable string; other types are dropped."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    text = "".join(ch for ch in value if ch.isprintable()).strip()
    return text[:limit] or None


def _addition_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def car_reported(data: Any, limit: int = 4) -> tuple[str, ...]:
    """The car's own causes for a refused item: the additionInfos with a non-zero value.

    A refused lock (Aion UT, door open) answers ``results[].itemInfos[{itemErrorCode
    13005, additionInfos[{additionDes "Driver door ajar status", additionValue "1.0"},
    {additionDes "BCM leave out remote mode reasons", additionValue "0.0"}]}]``.
    """
    found: list[str] = []
    results = data.get("results") if isinstance(data, dict) else None
    for result in results if isinstance(results, list) else ():
        infos = result.get("itemInfos") if isinstance(result, dict) else None
        for info in infos if isinstance(infos, list) else ():
            if not isinstance(info, dict) or str(info.get("itemErrorCode") or "0").strip() in ("0", ""):
                continue
            additions = info.get("additionInfos")
            for addition in additions if isinstance(additions, list) else ():
                if not isinstance(addition, dict):
                    continue
                text = _clean(addition.get("additionDes"), 80)
                value = _addition_value(addition.get("additionValue"))
                if not text or not value:
                    continue
                if value == 1 and text.lower().endswith(" status"):
                    text = text[:-len(" status")]
                elif value != 1:
                    text = f"{text}: {value:g}"
                if text not in found:
                    found.append(text)
    return tuple(found[:limit])


def interpret(payload: bytes | dict) -> PushResult:
    """Read a result message, as received (bytes) or already decoded (a polled reply)."""
    try:
        obj = payload if isinstance(payload, dict) else json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return PushResult(None, None, None, None)
    if not isinstance(obj, dict):
        return PushResult(None, None, None, obj)
    code = next((obj[key] for key in ("code", "resultCode", "status") if obj.get(key) is not None), None)
    ok: bool | None = None
    success = obj.get("success")
    if isinstance(success, bool):
        ok = success
    elif isinstance(code, (int, str)) and not isinstance(code, bool):
        key = str(code).strip().lower()
        if key in SUCCESS_CODES:
            ok = True
        elif key in FAILURE_CODES:
            ok = False
    msg = _clean(obj.get("msg") or obj.get("message") or obj.get("resultMsg"))
    data = obj.get("data", obj)
    ident = data.get("identifier") if isinstance(data, dict) else None
    ident = ident if isinstance(ident, dict) else {}
    upd = data.get("updateTime") if isinstance(data, dict) else None
    return PushResult(
        ok, normalize_id(code), msg, data,
        event=normalize_id(ident.get("event")),
        session_id=normalize_id(ident.get("sessionId")),
        vin=normalize_id(ident.get("vin")),
        update_time_ms=_millis(upd),
        reason=result_reason(normalize_id(code)) if ok is False else None,
        details=car_reported(data) if ok is False else (),
    )


class PushClient:
    """Keeps the command-result subscription open until :meth:`stop`."""

    def __init__(self, fetch_info: Callable[[], Awaitable[dict]], handler: MessageHandler,
                 decrypt: Callable[[dict], Any] | None = None,
                 tls_context: ssl.SSLContext | None = None, allow_plaintext: bool = False) -> None:
        self._fetch_info = fetch_info
        self._handler = handler
        self._decrypt = decrypt          # envelope -> plain object (messages arrive enveloped)
        self._tls_context = tls_context  # built once, off the event loop, when first needed
        # The service may hand out a plain-TCP broker; credentials on it are
        # short-lived, but a caller must opt in to sending them unencrypted.
        self._allow_plaintext = allow_plaintext
        self._stop = asyncio.Event()
        self.connected = False
        # The broker last connected to; on a plaintext broker its topics, client ID and
        # user name travel unencrypted.
        self.broker: BrokerInfo | None = None
        # True once the service offered only a plaintext broker and plaintext was not allowed:
        # the client then stops instead of asking for broker details every few minutes.
        self.refused_plaintext = False

    async def run(self) -> None:
        if aiomqtt is None:  # pragma: no cover
            raise GacError("aiomqtt is required for the push channel")
        delay = 5
        while not self._stop.is_set():
            connected_at: float | None = None
            try:
                info = BrokerInfo.from_data(await self._fetch_info())
                if not info.host or not info.topics:
                    raise GacError("push channel unavailable: no broker details")
                if not info.tls and not self._allow_plaintext:
                    # Retrying cannot help until the caller allows plaintext (a new client).
                    self.refused_plaintext = True
                    # Expected with plaintext off, so DEBUG; callers can show refused_plaintext.
                    _LOGGER.debug("Push channel not used: broker %s:%s is not TLS and plaintext is not "
                                  "allowed; command results must be polled", info.host, info.port)
                    return
                async with aiomqtt.Client(
                    info.host, port=info.port, username=info.username, password=info.password,
                    identifier=info.client_id, keepalive=60,
                    tls_context=await self._tls() if info.tls else None,
                ) as client:
                    for topic in info.topics:
                        await client.subscribe(topic)
                    self.connected = True
                    self.broker = info
                    connected_at = monotonic()
                    _LOGGER.debug("push channel connected (%d topic(s), tls=%s)", len(info.topics), info.tls)
                    await self._pump(client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                if connected_at is not None and monotonic() - connected_at >= STABLE_SECONDS:
                    delay = 5   # it held for a while: start the backoff over
                _LOGGER.debug("push channel down (%s); retry in %ss", exc, delay)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(delay * 2, 300)
            finally:
                self.connected = False

    async def _tls(self) -> ssl.SSLContext:
        if self._tls_context is None:
            loop = asyncio.get_running_loop()
            self._tls_context = await loop.run_in_executor(None, ssl.create_default_context)
        return self._tls_context

    async def _pump(self, client: Any) -> None:
        """Deliver messages until the connection drops or :meth:`stop` is called."""
        messages = client.messages.__aiter__()
        stop_task = asyncio.ensure_future(self._stop.wait())
        next_task: asyncio.Future | None = None
        try:
            while not self._stop.is_set():
                next_task = asyncio.ensure_future(messages.__anext__())
                done, _ = await asyncio.wait({next_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                if next_task not in done:
                    break
                try:
                    message = next_task.result()
                except StopAsyncIteration:
                    break
                payload = bytes(message.payload) if not isinstance(message.payload, bytes) else message.payload
                payload = self._unwrap(payload)
                try:
                    res = self._handler(str(message.topic), payload, interpret(payload))
                    if inspect.isawaitable(res):
                        await res
                except Exception:  # noqa: BLE001 — a handler bug must not drop the feed
                    _LOGGER.exception("push handler failed")
                next_task = None
        finally:
            pending = [t for t in (stop_task, next_task) if t is not None and not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    def _unwrap(self, payload: bytes) -> bytes:
        """Require a decryptable envelope when a decryptor was configured.

        An empty payload represents an untrusted result; interpret returns an
        unknown outcome. Encryption alone does not replace TLS authentication.
        """
        if self._decrypt is None:
            return payload
        try:
            obj = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return b""
        if isinstance(obj, dict) and obj.get("encryptData"):
            try:
                plain = self._decrypt(obj)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("could not open push message")
                return b""
            if plain is not None:
                return json.dumps(plain).encode()
        return b""

    def stop(self) -> None:
        self._stop.set()
