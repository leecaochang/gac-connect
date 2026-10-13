"""The async client: sign-in, session refresh, reads and commands.

    async with aiohttp.ClientSession() as http:
        client = GacClient("AU", http)
        captcha = await client.start_captcha()
        # ... user solves the puzzle, giving x ...
        await client.request_sms(mobile, x)
        await client.login_sms(mobile, code)
        vehicles = await client.list_vehicles()
        status = await client.get_status(vehicles[0].vin)

Design notes:

* Requests are encrypted + signed here; responses are decrypted here. Nothing
  else in the library does I/O.
* The IoV access token lasts ~1 h and is refreshed on demand; the refresh token
  is single-use, so every rotation is persisted before the retry. A spent
  refresh raises :class:`AuthExpiredError` (reauth).
* Vehicle commands are never auto-retried (a duplicate can double-actuate); reads
  are refreshed-and-retried once on an invalid token.
* Every request goes through one :class:`~gac_connect.limits.Limiter`, shared by
  all clients in the process unless another is given: one request at a time, at
  most one per MIN_REQUEST_GAP, within the rolling REQUEST_BUDGETS (and
  COMMAND_BUDGETS for commands), and nothing at all during a cool-down after a
  429. Over a limit the client raises RateLimitedError without sending.
* Token refreshes are serialised; a refresh that fails for good is remembered,
  so a dead session is never retried until the next sign-in.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import random
import re
import threading
import time
from contextlib import AsyncExitStack
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from . import commands, vehicle
from .auth import Captcha
from .const import (
    APP_VERSION,
    CODE_COMMAND_ACCEPTED,
    ERR_REFRESH_SPENT,
    ERR_TOKEN_INVALID,
    HTTP_TIMEOUT,
    IOV_APP_ID,
    IOV_VERSION,
    MAIN_APP_ID,
    PROFILE_IOV_APP_ID,
    PROFILE_MATERIAL,
    REGIONS,
    SSO_SERVICE_ID,
    SUCCESS_RESULT_CODES,
    USER_AGENT,
)
from .crypto import decrypt_envelope, encrypt_envelope, encrypt_sensitive
from .errors import (
    AuthExpiredError,
    CaptchaError,
    CommandError,
    GacError,
    LoginError,
    PinRequiredError,
    RateLimitedError,
    RegionError,
    ResponseError,
    TokenInvalidError,
    result_reason,
)
from .keys import Material, load_material
from .limits import DEFAULT_LIMITER, Limiter, parse_retry_after
from .models import Vehicle, VehicleStatus
from .phone import local_mobile
from .session import MemoryStore, Session, TokenStore
from .signing import IOV_FIELDS, MAIN_FIELDS, sign

# One token refresh at a time in the process, so clients sharing a session (and a
# store) never spend the same single-use refresh token twice.
_REFRESH_GATE = threading.Lock()
_LOGGER = logging.getLogger(__name__)


def _request_id() -> str:
    return f"{random.randint(0, 99_999_999):08d}"


def _now_ms() -> str:
    return str(int(time.time() * 1000))


def _complete_iov_tokens(data: Any) -> bool:
    """A partial rotation must never overwrite the last complete credential pair."""
    if not isinstance(data, dict):
        return False
    if not all(isinstance(data.get(key), str) and data[key].strip() for key in ("token", "refreshToken")):
        return False
    for key in ("expireTime", "rexpireTime"):
        value = data.get(key)
        if type(value) is int and value > 0:
            continue
        if isinstance(value, float) and math.isfinite(value) and value > 0:
            continue
        return False
    return True


class GacClient:
    def __init__(
        self,
        region: str,
        http: aiohttp.ClientSession,
        store: TokenStore | None = None,
        *,
        material: Material | None = None,
        limiter: Limiter | None = None,
        timezone: str | None = None,
    ) -> None:
        if region not in REGIONS:
            raise RegionError(f"unknown region {region!r}; known: {', '.join(REGIONS)}")
        self.region = region
        self._cfg = REGIONS[region]
        # Charge windows are given in local clock time, so a country spanning several
        # zones (RU, BR, MX, ID, AU, ES, PT, CL) needs the car's own, not the table's.
        self.timezone = timezone or self._cfg["tz"]
        self._tz: ZoneInfo | None = None
        if timezone is not None:
            self._zone()      # a zone the caller named is checked now, not at the first window
        self._http = http
        self._store = store or MemoryStore()
        # Each profile (GAC International, or a separate national app like My AION UK)
        # has its own key material; the region picks which bundle to load.
        profile = self._cfg.get("profile", "intl")
        self._m = material or load_material(PROFILE_MATERIAL[profile])
        # The IoV subsystem runs under a per-profile app id (My AION UK uses "3").
        self._iov_app_id = PROFILE_IOV_APP_ID.get(profile, IOV_APP_ID)
        self._session = Session(region=region)
        self._captcha: Captcha | None = None
        self._verify_ticket: str | None = None
        # Every request passes the process-wide limiter; a caller's own limiter (for
        # example one kept in a file) is applied on top of it, never instead of it.
        self._limiters = (limiter, DEFAULT_LIMITER) if limiter not in (None, DEFAULT_LIMITER) else (DEFAULT_LIMITER,)
        self._auth_dead = False      # a refresh failed for good; only a new session clears it
        self._dead_refresh: str | None = None
        self._charge_time_formats: dict[str, str] = {}
        self._charge_windows: dict[str, dict] = {}   # last usable reported reservation, per VIN

    def _zone(self) -> ZoneInfo:
        """The zone charge windows are scheduled in, loaded the first time one is.

        Reading status needs no time-zone database, so a system without one (a
        slim container, Windows without ``tzdata``) stays usable for everything
        except scheduling.
        """
        if self._tz is None:
            try:
                self._tz = ZoneInfo(self.timezone)
            except (KeyError, ValueError) as exc:
                raise RegionError(
                    f"time zone {self.timezone!r} is unavailable; check the name, or install "
                    "the 'tzdata' package if this system has no time-zone database"
                ) from exc
        return self._tz

    # ---- lifecycle -------------------------------------------------------
    async def load(self) -> None:
        """Restore a persisted session (call once before use)."""
        self._session = await self._store.load()
        self._session.region = self.region
        if self._session.expired:
            self._auth_dead = True      # stored as expired by this or another client
        elif self._auth_dead and self._session.refresh_token != self._dead_refresh:
            self._auth_dead = False     # a different (new) session was stored

    async def _persist(self) -> None:
        await self._store.save(self._session)

    @property
    def session(self) -> Session:
        return self._session

    # ---- low-level transport --------------------------------------------
    async def _post(self, host: str, path: str, wrapper: dict, headers: dict, kind: str = "request") -> Any:
        try:
            url = f"https://{host}{path}"
            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
            async with AsyncExitStack() as held:
                for limiter in self._limiters:
                    await held.enter_async_context(limiter.slot(kind))
                async with self._http.post(url, data=_dumps(wrapper), headers=headers, timeout=timeout,
                                           allow_redirects=False, raise_for_status=False) as resp:
                    if resp.status == 429:
                        seconds = parse_retry_after(resp.headers.get("Retry-After"))
                        pause = max(limiter.block(seconds) for limiter in self._limiters)
                        raise RateLimitedError("the service rate-limited the request", retry_after=pause)
                    if 300 <= resp.status < 400:
                        raise ResponseError(f"unexpected redirect ({resp.status}) from {path}; not followed")
                    if resp.status >= 400:
                        body = self._error_body(host, await resp.read())
                        _LOGGER.debug("HTTP %s from %s: %s", resp.status, path, _excerpt(body))
                        code = _safe_result_code(body.get("code")) if isinstance(body, dict) else None
                        detail = f" (result code {code})" if code else ""
                        raise ResponseError(f"HTTP {resp.status} from {path}{detail}")
                    raw = await resp.read()
            try:
                outer = _loads(raw)
            except ValueError as exc:
                raise ResponseError(f"non-JSON response ({resp.status}) from {path}") from exc
            if isinstance(outer, dict) and outer.get("encryptData"):
                plain = decrypt_envelope(outer, self._response_key(host))
                if plain is not None:
                    return plain
            return outer
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ResponseError("Could not reach GAC") from err

    def _error_body(self, host: str, raw: bytes) -> Any:
        """An HTTP error reply: decoded (and decrypted) JSON, or its text."""
        raw = raw[:65536]
        try:
            body = _loads(raw)
        except ValueError:
            return raw.decode("utf-8", errors="replace")
        if isinstance(body, dict) and body.get("encryptData"):
            body = decrypt_envelope(body, self._response_key(host)) or body
        return body

    def _response_key(self, host: str):
        return (self._m.main if host == self._cfg["main"] else self._m.iov_response or self._m.iov).private

    async def _main_call(self, path: str, body: Any, *, authorization: str | None = None) -> Any:
        wrapper, _, _ = encrypt_envelope(body, self._m.main.public)
        headers = {
            "user-agent": USER_AGENT, "fnc-app-type": "android", "locale": "en",
            "fnc-fnc-os-version-type": APP_VERSION, "fnc-app-id": MAIN_APP_ID,
            "fnc-timestamp": _now_ms(), "region": self.region,
            "fnc-client-timezone": self.timezone, "fnc-requestId": _request_id(),
            "content-type": "application/json; charset=utf-8",
        }
        if authorization:
            headers["Authorization"] = authorization
        headers["sig"] = sign(headers, wrapper, fields=MAIN_FIELDS, hmac_key=self._m.main_hmac)
        return await self._post(self._cfg["main"], f"/gateway/v1{path}", wrapper, headers)

    async def _iov_call(self, path: str, body: Any, *, sensitive: dict[str, str] | None = None,
                        kind: str = "request") -> Any:
        wrapper, key, iv = encrypt_envelope(body, self._m.iov.public)
        headers = {
            "user-agent": USER_AGENT, "fnc-app-type": "android", "locale": "en",
            "fnc-fnc-os-version-type": APP_VERSION, "fnc-request-id": _request_id(),
            "content-type": "application/json; charset=utf-8", "fnc-version": IOV_VERSION,
            "fnc-app-id": self._iov_app_id, "fnc-timestamp": _now_ms(),
            "fnc-icv-timezone": self.timezone,
        }
        if self._session.token:
            headers["token"] = self._session.token
        if sensitive:
            for name, value in sensitive.items():
                headers[name] = encrypt_sensitive(value, key, iv)
            headers["fnc-sensitive-fields"] = ",".join(sensitive)
        headers["sig"] = sign(headers, wrapper, fields=IOV_FIELDS, hmac_key=self._m.iov_hmac)
        return await self._post(self._cfg["iov"], f"/gateway{path}", wrapper, headers, kind)

    @staticmethod
    def _data(resp: Any) -> Any:
        return resp.get("data") if isinstance(resp, dict) else None

    @staticmethod
    def _ok(resp: Any) -> bool:
        return isinstance(resp, dict) and resp.get("success") is True

    # ---- sign-in ---------------------------------------------------------
    async def start_captcha(self) -> Captcha:
        resp = await self._main_call("/support/api/captcha/v1/get/sec", {"captchaType": "blockPuzzle"})
        if not self._ok(resp):
            raise CaptchaError(f"could not start captcha: {_msg(resp)}")
        self._captcha = Captcha.from_data(resp["data"])
        return self._captcha

    async def _verify_captcha(self, x: int) -> Captcha:
        """Check the slide; on success the same Captcha carries the proofs."""
        if self._captcha is None:
            raise CaptchaError("call start_captcha first")
        c = self._captcha
        resp = await self._main_call("/support/api/captcha/v1/check/sec", {
            "captchaType": "blockPuzzle", "pointJson": c.point_json(x), "token": c.token,
        })
        if not self._ok(resp):
            raise CaptchaError("puzzle position rejected; fetch a new puzzle and retry")
        return c

    async def request_sms(self, mobile: str, x: int) -> None:
        """Verify the puzzle at offset ``x`` and send a login code to ``mobile``."""
        mobile = local_mobile(mobile, self._cfg["tel"])
        c = await self._verify_captcha(x)
        resp = await self._main_call(
            "/support/api/message/verify-code/sms/send-verify-code/sec",
            {"msgBizCode": "userLogin", "countryTelCode": self._cfg["tel"],
             "mobile": mobile, "email": "", "captchaType": "blockPuzzle",
             "captchaTicket": c.ticket(x), "captchaRandomStr": c.point_json(x),
             "type": 1, "sendType": None},
        )
        data = self._data(resp)
        ticket = data.get("verifyCodeTicket") if isinstance(data, dict) else None
        if not self._ok(resp) or not isinstance(ticket, str) or not ticket:
            raise LoginError(f"could not send SMS code: {_msg(resp)}")
        self._verify_ticket = ticket

    async def login_sms(self, mobile: str, code: str) -> None:
        """Complete sign-in with the SMS code, then mint an IoV session."""
        ticket = self._verify_ticket
        if not ticket:
            raise LoginError("request_sms must succeed first")
        resp = await self._main_call("/iam/api/user/login/code/sec", {
            "verifyCodeTicket": ticket, "verifyCode": code, "type": 1,
            "countryTelCode": self._cfg["tel"], "mobile": local_mobile(mobile, self._cfg["tel"]), "email": "",
            "protocolList": ["COMMON_PRIVACY_POLICY", "COMMON_USER_AGREEMENT"],
        })
        d = self._data(resp)
        if not self._ok(resp) or not isinstance(d, dict) or not isinstance(d.get("token"), str) or not d["token"]:
            raise LoginError(f"code login failed: {_msg(resp)}")
        self._session.main_token = d.get("token")
        self._session.main_refresh_token = d.get("refreshToken")
        await self._establish_iov()

    async def _establish_iov(self) -> None:
        """Exchange the main token for an IoV ticket, then an IoV session."""
        resp = await self._main_call(
            "/iam/sso/login/sec", {"serviceId": SSO_SERVICE_ID},
            authorization=self._session.main_token,
        )
        ticket = self._data(resp)
        if isinstance(ticket, dict):
            ticket = ticket.get("value")
        if not self._ok(resp) or not isinstance(ticket, str):
            raise LoginError(f"SSO ticket exchange failed: {_msg(resp)}")
        sso = await self._iov_call("/sso/login", {"ticket": ticket})
        d = self._data(sso)
        if not (isinstance(d, dict) and d.get("token")):
            raise LoginError(f"IoV session exchange failed: {_msg(sso)}")
        self._apply_iov_tokens(d)
        self._session.expired = False
        self._auth_dead, self._dead_refresh = False, None
        await self._persist()
        await self._rotate_account_token()

    async def _rotate_account_token(self) -> None:
        """Refresh the account token once, now that it has minted the vehicle session.

        A vehicle session is ended along with the account token that minted it: by
        a logout, or by a sign-in elsewhere replacing it, such as the official app
        signing in. Once that token has been refreshed away, ending its successor no
        longer reaches the vehicle session. The app refreshes its account token as a
        matter of course, which is why sessions it minted outlive later sign-ins;
        without this, one minted here was ended by the next app sign-in. Best
        effort: if it fails, the signed-in session is kept as it was.
        """
        if not (self._session.main_token and self._session.main_refresh_token):
            return
        try:
            resp = await self._main_call(
                "/iam/api/user/token/refresh/sec", {"refreshToken": self._session.main_refresh_token},
                authorization=self._session.main_token,
            )
        except (GacError, aiohttp.ClientError, TimeoutError):
            _LOGGER.warning("Account rotation failed; sign-in is usable but later app sign-in may end it")
            return
        d = self._data(resp)
        if self._ok(resp) and isinstance(d, dict) and d.get("token"):
            self._session.main_token = d["token"]
            self._session.main_refresh_token = d.get("refreshToken") or self._session.main_refresh_token
            await self._persist()

    def _apply_iov_tokens(self, d: dict) -> None:
        self._session.token = d.get("token")
        self._session.refresh_token = d.get("refreshToken")
        self._session.expire_time = d.get("expireTime")
        self._session.rexpire_time = d.get("rexpireTime")

    async def _ensure_token(self) -> None:
        """Make sure the IoV token is usable; never sends anything for a dead session."""
        if self._auth_dead:
            raise AuthExpiredError("session expired; sign in again")
        if not self._session.access_valid:
            await self._refresh()

    async def _refresh(self, stale_token: str | None = None) -> None:
        """Rotate the IoV tokens once, however many callers need it at the same time.

        ``stale_token`` is the token a request was rejected with; if another caller
        has already replaced it, nothing is sent. The rotated pair is saved before
        anything uses it.
        """
        while not _REFRESH_GATE.acquire(blocking=False):
            await asyncio.sleep(0.02)
        try:
            if self._auth_dead:
                raise AuthExpiredError("session expired; sign in again")
            stored = await self._store.load()     # another client on this store may have rotated
            if stored.expired and not stored.refresh_token:
                self._auth_dead, self._dead_refresh = True, self._session.refresh_token
                raise AuthExpiredError("session expired; sign in again")
            if (stored.token and stored.access_valid and stored.token != self._session.token
                    and stored.refresh_token != self._session.refresh_token):
                stored.region = self.region
                self._session = stored
                return
            if stale_token is None and self._session.access_valid:
                return
            if stale_token is not None and self._session.token != stale_token:
                return
            try:
                if not self._session.refresh_valid:
                    raise AuthExpiredError("refresh token expired; sign in again")
                resp = await self._iov_call("/refresh/token", {"refreshToken": self._session.refresh_token})
                d = self._data(resp)
                if isinstance(resp, dict) and resp.get("code") == ERR_REFRESH_SPENT:
                    # Not time-expired (refresh_valid passed above), so the gateway has
                    # retired this token: it was used already, or the session was ended
                    # server-side. The official app signing in has been seen to end a
                    # session this library minted minutes earlier, so name both causes.
                    raise AuthExpiredError(
                        "the gateway rejected this refresh token: it was already used (another client "
                        "on the same saved session), or the session was ended by a sign-in elsewhere, "
                        "such as the official app. Sign in again to recover"
                    )
                if not _complete_iov_tokens(d):
                    raise ResponseError("refresh response has no usable token; credentials retained")
            except AuthExpiredError:
                self._auth_dead, self._dead_refresh = True, self._session.refresh_token
                # Store the dead session without its tokens, so other clients on this
                # store and later runs fail locally instead of trying it again.
                try:
                    await self._store.save(dataclasses.replace(
                        self._session, token=None, refresh_token=None, expire_time=None, rexpire_time=None,
                        expired=True))
                except Exception as exc:  # noqa: BLE001 — the failure below is what matters
                    self._limiters[-1].warn("could not store the expired session: %s", exc)
                raise
            rotated = dataclasses.replace(self._session, token=d.get("token"), refresh_token=d.get("refreshToken"),
                                          expire_time=d.get("expireTime"), rexpire_time=d.get("rexpireTime"))
            try:
                await self._store.save(rotated)   # saved BEFORE any request uses it
            finally:
                self._session = rotated           # the old pair is spent either way
        finally:
            _REFRESH_GATE.release()

    # ---- reads -----------------------------------------------------------
    async def list_vehicles(self) -> list[Vehicle]:
        """Vehicles bound to or shared with the account; a failed query raises instead of returning none."""
        resp = await self._main_call("/icv/api/my-vehicle/list/sec", {},
                                     authorization=self._session.main_token)
        d = self._data(resp)
        if not self._ok(resp) or not isinstance(d, dict):
            safe_code = _safe_result_code(resp.get("code") if isinstance(resp, dict) else None)
            detail = f"result code {safe_code}" if safe_code is not None else "no recognized result code"
            raise ResponseError(f"vehicle list was not accepted ({detail})")
        records = [r for key in ("bindVehicles", "authorizedVehicles")
                   if isinstance(d.get(key), list) for r in d[key]]
        vehicles = [Vehicle.from_record(r) for r in records if isinstance(r, dict)]
        return [v for v in vehicles if isinstance(v.vin, str) and v.vin.strip()]

    async def get_status(self, vin: str) -> VehicleStatus:
        resp = await self._iov_read(commands.CATALOG["status"].path, {"vin": vin}, sensitive={"vin": vin})
        data = self._data(resp)
        reports = data.get("results") if isinstance(data, dict) else None
        if not isinstance(reports, list) or not reports or not isinstance(reports[0], dict):
            raise ResponseError("vehicle status response has no valid report")
        status = VehicleStatus.from_results(reports[0], online=data.get("online"), updated_ms=data.get("updateTime"))
        if status.charge_time_format is not None:
            self._charge_time_formats[vin] = status.charge_time_format
        charging = reports[0].get("chargingAccessors")
        if (window := vehicle.reported_window(charging[0] if isinstance(charging, list) and charging else None)):
            previous = self._charge_windows.get(vin, {})
            if "chargingMode" not in window and "chargingMode" in previous:
                window["chargingMode"] = previous["chargingMode"]   # free now; remember how it was scheduled
            self._charge_windows[vin] = window
        return status

    async def get_position(self, vin: str) -> dict:
        resp = await self._iov_read(commands.CATALOG["position"].path, {"vin": vin}, sensitive={"vin": vin})
        data = self._data(resp)
        if not isinstance(resp, dict) or resp.get("success") is False or not isinstance(data, dict):
            safe_code = _safe_result_code(resp.get("code") if isinstance(resp, dict) else None)
            detail = f"result code {safe_code}" if safe_code is not None else "no valid data"
            raise ResponseError(f"vehicle position response was not accepted ({detail})")
        return data

    async def get_control_capabilities(self, vin: str) -> dict:
        """Read the app's raw capability response without issuing a control.

        This endpoint uses ``v1/iov/vehicle``, unlike status/control paths.
        Numeric capability flags are not yet calibrated; this response must
        not automatically enable controls or establish a physical effect.
        """
        resp = await self._iov_read(
            "/iov-vehicle-gateway/v1/iov/vehicle/access_gateway/get_capability",
            {"vin": vin}, sensitive={"vin": vin},
        )
        data = self._data(resp)
        code = resp.get("code") if isinstance(resp, dict) else None
        # The app's generic read wrapper accepts "0" and other normal success
        # codes, without requiring the main API's success boolean. Keep async
        # command-only codes out of this metadata read and do not broaden _ok.
        success_code = type(code) in (int, str) and str(code) in SUCCESS_RESULT_CODES
        if (not isinstance(resp, dict) or resp.get("success") is False
                or not (success_code or (code is None and self._ok(resp)))):
            safe_code = _safe_result_code(code)
            detail = f"result code {safe_code}" if safe_code is not None else "no recognized result code"
            raise ResponseError(f"vehicle capability query was not accepted ({detail})")
        if not isinstance(data, dict):
            raise ResponseError("vehicle capability response has no valid data")
        return data

    async def _iov_read(self, path: str, body: Any, *, sensitive: dict | None = None) -> Any:
        """A read that refreshes-and-retries once on an invalid token."""
        await self._ensure_token()
        used = self._session.token
        resp = await self._iov_call(path, body, sensitive=sensitive)
        if isinstance(resp, dict) and resp.get("code") == ERR_TOKEN_INVALID:
            await self._refresh(stale_token=used)
            resp = await self._iov_call(path, body, sensitive=sensitive)
        return resp

    # ---- push channel ----------------------------------------------------
    async def mqtt_info(self) -> dict:
        """Broker details for the command-result feed (short-lived password)."""
        resp = await self._iov_read("/mqtt/info", {})
        data = self._data(resp)
        if not isinstance(data, dict) or not (data.get("host") or data.get("connectHost")):
            raise ResponseError(f"push channel details unavailable: {_msg(resp)}")
        return data

    def decrypt_push(self, envelope: dict) -> Any:
        """Open a message from the push feed (same envelope as gateway responses)."""
        return decrypt_envelope(envelope, (self._m.iov_response or self._m.iov).private)

    # ---- commands --------------------------------------------------------
    async def command(self, vin: str, name: str, **overrides: Any) -> Any:
        cmd = commands.CATALOG.get(name)
        if cmd is None:
            raise CommandError(f"unknown command {name!r}")
        if cmd.pin:
            raise PinRequiredError(f"{name} needs the remote-control PIN, not yet supported")
        if name == "vehicle-power-off" and overrides:
            raise CommandError("vehicle shutdown accepts no parameters")
        body = commands.build_body(cmd, vin, overrides)
        return await self._dispatch_command(cmd.path, body, vin)

    async def _dispatch_command(self, path: str, body: dict, vin: str) -> Any:
        await self._ensure_token()
        used = self._session.token
        resp = await self._iov_call(path, body, sensitive={"vin": vin}, kind="command")
        try:
            return _command_result(resp)
        except TokenInvalidError:
            # Never replay an action. Prepare only the next explicit attempt, and
            # do not invalidate a token another caller has already rotated.
            if self._session.token == used:
                self._session.expire_time = 0
            raise

    async def climate_on(self, vin: str, *, temperature: float = 24.0, minutes: int = 30) -> Any:
        """Run the cabin A/C in auto mode at ``temperature`` °C for ``minutes``.

        Raises ``ValueError`` for a non-finite or out-of-range temperature (18–32 °C,
        rounded to 0.5) or duration (5–60 minutes) before anything is sent.
        """
        t, m = commands.validate_climate(temperature, minutes)
        return await self.command(vin, "aircon-on", temperature=t, currentDuration=m)

    async def climate_manual_on(
        self, vin: str, *, temperature: float = 24.0, minutes: int = 30,
        fan_speed: int | None = None, blowing_mode: int | None = None,
    ) -> Any:
        """Start manual A/C, optionally selecting the blower or outlet code.

        Omitted fan_speed/blowing_mode fields preserve the vehicle's settings.
        A selection starts or renews the timed run at the supplied temperature.
        Fan speed uses 1–10; outlet codes must be calibrated for the vehicle.
        Manual startup and fan writes were verified on an Aion UT. Outlet codes
        1–4 worked with that car fully on (ready to drive); 3 had no effect
        before then and 5 was rejected. Other codes/models remain unverified.
        """
        target, duration = commands.validate_climate(temperature, minutes)
        params = {"temperature": target, "currentDuration": duration}
        if fan_speed is not None:
            params["windStrength"] = commands.validate_fan_speed(fan_speed)
        if blowing_mode is not None:
            params["blowingMode"] = commands.validate_blowing_mode(blowing_mode)
        return await self.command(vin, "aircon-manual-on", **params)

    async def climate_off(self, vin: str) -> Any:
        return await self.command(vin, "aircon-off")

    async def climate_set_temperature(self, vin: str, temperature: float) -> Any:
        """Adjust running A/C without requesting Auto or changing fan/airflow fields.

        This partial operation is best-effort and needs vehicle confirmation.
        It does not send a duration or automatically retry as a full A/C start.
        """
        target, _ = commands.validate_climate(temperature, 30)
        return await self.command(vin, "aircon-temperature", temperature=target)

    async def set_seat_ventilation(self, vin: str, seat: int, enabled: bool) -> Any:
        """Toggle ventilation without changing seat heating or cabin settings.

        Seat IDs follow the app: 1 driver, 2 front passenger, 3 rear left,
        4 rear right. Only use seats fitted with ventilation. The app's binary
        controls send windStrength 1 for on and 0 for off, both with enableWind 1.
        An accepted request still needs a result or a fresh vehicle report.
        """
        if type(seat) is not int or seat not in (1, 2, 3, 4):
            raise ValueError("seat must be an integer from 1 to 4")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        return await self.command(vin, f"seat-{seat}-ventilation-{'on' if enabled else 'off'}")

    async def fridge_on(self, vin: str, *, mode: str = "refrigerate", temperature: float | None = None) -> Any:
        """Ask the car to run the fridge in ``mode`` ("refrigerate", "heat" or "freeze") at ``temperature`` °C.

        Without a temperature this library's default for the mode is sent
        (refrigerate 3, heat 42, freeze -12 °C), so omitting it while the fridge
        runs resets the target. Raises ``ValueError`` for an unknown mode or an
        out-of-range temperature before anything is sent. Sending this while the
        fridge runs changes its mode or temperature. An accepted request is not
        proof the car applied it; read the status to confirm.
        """
        code, t = commands.validate_fridge(mode, temperature)
        return await self.command(vin, "fridge-on", workingMode=code, refrigeratorTemperature=t)

    async def fridge_off(self, vin: str) -> Any:
        return await self.command(vin, "fridge-off")

    async def lock(self, vin: str) -> Any:
        return await self.command(vin, "lock")

    async def unlock(self, vin: str) -> Any:
        """Unlock the doors without a PIN; send once and validate acceptance."""
        return await self.command(vin, "unlock")

    async def charge_now(self, vin: str, *, until_soc: bool = False) -> Any:
        """Allow charging whenever plugged in; the car keeps its saved window for a later resume."""
        return await self._reservation(vin, vehicle.charge_now_operation(until_soc=until_soc))

    async def resume_schedule(self, vin: str, *, until_soc: bool | None = None) -> Any:
        """Return to scheduled charging with the window the car last reported.

        This is not a continuous pause: the car charges inside that window. The
        last reported scheduled mode and stopping rule are kept, unless
        ``until_soc`` is given; with no scheduled report seen (for example the
        schedule was switched off before this client started), loop mode is
        used. Raises CommandError, without sending, before any status read has
        reported a usable window.
        """
        window = self._charge_windows.get(vin)
        if window is None:
            raise CommandError("no reported charge window to resume; read the status or set a charge window")
        mode = window.get("chargingMode", vehicle.MODE_LOOP)
        return await self._reservation(vin, vehicle.preserved_operation(window, mode, until_soc=until_soc,
                                                                        tz=self._zone()))

    async def charge_pause(self, vin: str) -> Any:
        """Unavailable: scheduling a daily window is not a continuous pause."""
        raise CommandError(vehicle.PAUSE_UNSUPPORTED)

    async def set_charge_window(self, vin: str, start: str, stop: str, *,
                                weekly: int = 0, until_soc: bool = False, once: bool = False) -> Any:
        """Schedule charging on the ``weekly`` days (0 = every day), or ``once`` for the next occurrence.

        Uses the time representation learned by get_status for this vehicle;
        before any usable report, the milliseconds-of-day format. A timestamp
        request uses the configured scheduling zone for both dates and
        timestamps. Acceptance still needs vehicle confirmation.
        """
        op = vehicle.window_operation(start, stop, self._zone(),
                                      weekly=weekly, until_soc=until_soc, once=once,
                                      timestamp_times=self._charge_time_formats.get(vin) == "timestamp")
        return await self._reservation(vin, op)

    async def _reservation(self, vin: str, operation: dict) -> Any:
        return await self._dispatch_command(vehicle._RESERVATION, vehicle.reservation_body(vin, operation), vin)


def command_session_id(resp: Any) -> str | None:
    """The session id the service assigns to an accepted command, if it gives one."""
    from .push import normalize_id  # same normalisation as push results, so ids correlate

    data = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(data, dict):
        return None
    ident = data.get("identifier")
    sid = data.get("sessionId") or (ident.get("sessionId") if isinstance(ident, dict) else None)
    return normalize_id(sid)


def _command_result(resp: Any) -> Any:
    """13001 "ongoing" is an accepted async command; the app's success codes are accepted too."""
    if isinstance(resp, dict):
        code = resp.get("code")
        if code == ERR_TOKEN_INVALID:
            raise TokenInvalidError("token invalid; refresh and reissue the command")
        text = str(code) if type(code) in (int, str) else None
        # An explicit success=false outweighs a generic success code, never the async acceptance code.
        if (resp.get("success") is True or text == str(CODE_COMMAND_ACCEPTED)
                or (text in SUCCESS_RESULT_CODES and resp.get("success") is not False)):
            return resp
        reason = result_reason(code)
        detail = f"{reason} (code {code})" if reason else _msg(resp)
        raise CommandError(f"command rejected: {detail}", code=code)
    raise CommandError("command response must be an object with an acceptance result")


def _msg(resp: Any) -> str:
    if isinstance(resp, dict):
        return str(resp.get("msg") or resp.get("code") or resp)[:200]
    return str(resp)[:200]


def _safe_result_code(value: Any) -> str | None:
    """Keep only a bounded gateway code, never an arbitrary response message."""
    if type(value) not in (int, str):
        return None
    code = str(value)
    return code if re.fullmatch(r"(?:[0-9]{1,8}|[A-Z]{1,12}(?:\.[A-Z]{1,12})?\.[0-9]{1,8})", code) else None


def _excerpt(body: Any, limit: int = 400) -> str:
    """A short, printable debug excerpt of an error reply, with VIN-like values masked."""
    import json
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str)
    text = "".join(ch for ch in text if ch.isprintable())
    return re.sub(r"\b[A-HJ-NPR-Z0-9]{17}\b", "<VIN>", text)[:limit]


def _dumps(obj: Any) -> bytes:
    import json
    return json.dumps(obj, separators=(",", ":")).encode()


def _loads(raw: bytes) -> Any:
    import json
    return json.loads(raw)
