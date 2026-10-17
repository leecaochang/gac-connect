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

* HTTP requests to GAC are encrypted and signed here, and their replies decrypted
  here; a reply that cannot be decrypted is an error, never taken at face value.
  Other I/O lives in its own module: the push feed (``push``), saved sessions
  (``session``), the request-limit record (``limits``) and the bundled keys (``keys``).
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
  so a dead session is never retried until the next sign-in. With a store that
  compares and saves in one step (``replace``, as every bundled store does), a
  refresh, renewal or account-token refresh never writes over a sign-in saved
  meanwhile; it takes that one instead.
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
    IOV_POLL_RESULT,
    IOV_VERSION,
    MAIN_APP_ID,
    PROFILE_IOV_APP_ID,
    PROFILE_IOV_REQUEST_KEY_SIZE,
    PROFILE_MATERIAL,
    REGIONS,
    SSO_SERVICE_ID,
    SUCCESS_RESULT_CODES,
    USER_AGENT,
)
from .crypto import decrypt_envelope, encrypt_envelope, encrypt_sensitive
from .errors import (
    AccountSessionEnded,
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
from .session import MemoryStore, Session, TokenStore, credential_pair
from .signing import IOV_FIELDS, MAIN_FIELDS, sign

# One token refresh at a time in the process, so clients sharing a session (and a
# store) never spend the same single-use refresh token twice.
_REFRESH_GATE = threading.Lock()
_LOGGER = logging.getLogger(__name__)
# Account-API codes on which GAC 2.0.32 (response interceptor 0xdbe2c0) leaves the account:
# 4402 and 4421 sign out (an expired token, which a refresh may still mend); 4423 opens the
# sign-in page with GAC's "kickMessage" (the account signed in on another phone). A refresh
# after 4423 could take the session back from the app, so it is not tried.
ACCOUNT_TOKEN_EXPIRED = frozenset({"4402", "4421"})
ACCOUNT_SIGNED_IN_ELSEWHERE = "4423"
ACCOUNT_SESSION_ENDED = ACCOUNT_TOKEN_EXPIRED | {ACCOUNT_SIGNED_IN_ELSEWHERE}


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


def _iov_fields(d: dict) -> dict:
    """A vehicle-session reply's tokens as Session fields."""
    return {"token": d.get("token"), "refresh_token": d.get("refreshToken"),
            "expire_time": d.get("expireTime"), "rexpire_time": d.get("rexpireTime")}


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
        self._iov_request_key_size = PROFILE_IOV_REQUEST_KEY_SIZE.get(profile, 32)
        self._session = Session(region=region)
        self._base: tuple | None = None    # the saved pair as this client last saw it (None: not read yet)
        self._changing = asyncio.Lock()     # one session change (build and save) at a time
        # (time.time(), accepted) of the last account-token refresh, for diagnostics.
        self.last_account_refresh: tuple[float, bool] | None = None
        self._captcha: Captcha | None = None
        self._verify_ticket: str | None = None
        # Every request passes the process-wide limiter; a caller's own limiter (for
        # example one kept in a file) is applied on top of it, never instead of it.
        self._limiters = (limiter, DEFAULT_LIMITER) if limiter not in (None, DEFAULT_LIMITER) else (DEFAULT_LIMITER,)
        self._auth_dead = False      # a refresh failed for good; only a new session clears it
        self._dead_refresh: str | None = None
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
        self._base = credential_pair(self._session)
        self._session.region = self.region
        if self._session.expired:
            self._auth_dead = True      # stored as expired by this or another client
        elif self._auth_dead and self._session.refresh_token != self._dead_refresh:
            self._auth_dead = False     # a different (new) session was stored

    async def _persist(self, *, sign_in: bool = False) -> bool:
        async with self._changing:
            return await self._commit(self._session, sign_in=sign_in)

    async def _change(self, *, sign_in: bool = False, **fields: Any) -> bool:
        """Change some session fields and save the result, one change at a time.

        The new session is built from the current one when its turn comes, so a vehicle-token
        rotation and an account-token refresh that overlap keep each other's new tokens.
        """
        async with self._changing:
            return await self._commit(dataclasses.replace(self._session, **fields), sign_in=sign_in)

    async def _commit(self, session: Session, *, sign_in: bool = False) -> bool:
        """Save ``session`` unless a new sign-in replaced the saved credentials meanwhile.

        The store compares and saves as one step (``replace``), against the credentials this
        client last loaded or saved. If they were replaced, this client takes the new ones and
        nothing is written (False). The step runs to its end even if the caller is cancelled,
        so a rotated pair, whose predecessor the service has already spent, is never lost.
        A sign-in (``sign_in``) is the newest session by definition and is always saved, as is
        anything from a client that has not read the store yet.
        """
        replace = getattr(self._store, "replace", None)

        async def work() -> Session | None:
            if replace is None or sign_in or self._base is None:
                await self._store.save(session)
                return None
            return await replace(self._base, session)

        task = asyncio.ensure_future(work())
        try:
            newer = await asyncio.shield(task)
        except asyncio.CancelledError:
            newer = await task
            self._settle(session, newer)
            raise
        return self._settle(session, newer)

    def _settle(self, session: Session, newer: Session | None) -> bool:
        if newer is None:
            self._session, self._base = session, credential_pair(session)
            return True
        newer.region = self.region
        self._session, self._base = newer, credential_pair(newer)
        self._auth_dead, self._dead_refresh = bool(newer.expired), None
        _LOGGER.info("Newer saved credentials (a new sign-in) replaced these; using them")
        return False

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
            if isinstance(outer, dict) and ("encryptData" in outer or "encryptKey" in outer):
                # An envelope, whatever its fields hold: it must decrypt (empty or null fails).
                plain = decrypt_envelope(outer, self._response_key(host))
                if plain is None:
                    # Never fall back to the outer fields: they are not the service's answer.
                    raise ResponseError(f"encrypted response from {path} could not be decrypted")
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
        # Requests are sealed to the same keypair the gateway encrypts IoV
        # responses with, not a separate request keypair; only the AES key size
        # differs per profile (see PROFILE_IOV_REQUEST_KEY_SIZE).
        wrapper, key, iv = encrypt_envelope(
            body, self._m.iov_response.public, key_size=self._iov_request_key_size
        )
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
        await self._establish_iov(sign_in=True)

    async def _establish_iov(self, *, sign_in: bool = False) -> None:
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
        # As with a refresh, only a complete session replaces the one held (and saved).
        if (isinstance(sso, dict) and sso.get("success") is False) or not _complete_iov_tokens(d):
            raise LoginError(f"IoV session exchange failed: {_msg(sso)}")
        self._auth_dead, self._dead_refresh = False, None
        if not await self._change(sign_in=sign_in, expired=False, **_iov_fields(d)):
            return          # a new sign-in was saved meanwhile; it is used instead
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
            await self._refresh_account_token()
        except (GacError, aiohttp.ClientError, TimeoutError):
            _LOGGER.warning("Account rotation failed; sign-in is usable but later app sign-in may end it")

    async def _refresh_account_token(self) -> bool:
        """Replace the account token with a fresh one; False if the service refuses."""
        resp = await self._main_call(
            "/iam/api/user/token/refresh/sec", {"refreshToken": self._session.main_refresh_token},
            authorization=self._session.main_token,
        )
        d = self._data(resp)
        accepted = bool(self._ok(resp) and isinstance(d, dict) and isinstance(d.get("token"), str)
                        and d["token"].strip() and isinstance(d.get("refreshToken") or "", str))
        self.last_account_refresh = (time.time(), accepted)
        if not accepted:
            _LOGGER.info("GAC refused to refresh the account token (result code %s)",
                         _safe_result_code(resp.get("code") if isinstance(resp, dict) else None))
            return False
        _LOGGER.info("Refreshed GAC's account token")
        return await self._change(main_token=d["token"],
                                  main_refresh_token=d.get("refreshToken") or self._session.main_refresh_token)

    async def _account_call(self, path: str, body: Any) -> Any:
        """An account-API call; an expired account token is refreshed once and the call repeated.

        GAC 2.0.32 signs out on 4402 and 4421 from these calls. Only the account session
        has run out then; the vehicle session it minted keeps working. 4423 (signed in on
        another phone) is returned as it is, never refreshed.
        """
        resp = await self._main_call(path, body, authorization=self._session.main_token)
        code = resp.get("code") if isinstance(resp, dict) else None
        if (type(code) in (int, str) and str(code) in ACCOUNT_TOKEN_EXPIRED and self._session.main_refresh_token
                and await self._refresh_account_token()):
            resp = await self._main_call(path, body, authorization=self._session.main_token)
        return resp

    async def _renew_from_account(self) -> bool:
        """Mint a new vehicle session from the saved account session, without signing in again.

        The vehicle session can run out or be retired while the account session that
        minted it still works. If the ticket exchange is refused, the account token is
        refreshed once and the exchange tried again. False means the account session
        is gone too (or was never saved), so a new sign-in is needed. Network and
        rate-limit errors propagate, so the session is not given up over them.
        """
        if not (self._session.main_token and self._session.main_refresh_token):
            return False
        for attempt in range(2):
            try:
                await self._establish_iov()
            except LoginError:
                if attempt or not await self._refresh_account_token():
                    return False
            else:
                _LOGGER.info("Renewed the vehicle session from the saved account session; no sign-in needed")
                return True
        return False


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
                self._session, self._base = stored, credential_pair(stored)
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
                if isinstance(resp, dict) and resp.get("success") is False:
                    raise ResponseError("the refresh was refused (result code "
                                        f"{_safe_result_code(resp.get('code'))}); credentials retained")
                if not _complete_iov_tokens(d):
                    raise ResponseError("refresh response has no usable token; credentials retained")
            except AuthExpiredError:
                if await self._renew_from_account():
                    return      # renewed, or a new sign-in was found while saving
                dead_refresh = self._session.refresh_token
                # Store the dead session without its tokens, so other clients on this
                # store and later runs fail locally instead of trying it again; unless a
                # new sign-in was saved meanwhile, which is then used instead.
                try:
                    if not await self._change(token=None, refresh_token=None, expire_time=None,
                                              rexpire_time=None, expired=True) and not self._session.expired:
                        return
                except Exception as exc:  # noqa: BLE001 — the failure below is what matters
                    self._limiters[-1].warn("could not store the expired session: %s", exc)
                self._auth_dead, self._dead_refresh = True, dead_refresh
                raise
            try:
                # Saved BEFORE any request uses it, unless a new sign-in was saved while
                # this refresh was out (that one is then used).
                await self._change(**_iov_fields(d))
            except asyncio.CancelledError:
                raise           # the save ran to its end and its outcome was kept (_commit)
            except BaseException:
                # The save itself failed: the old pair is spent, so hold the rotated one.
                self._session = dataclasses.replace(self._session, **_iov_fields(d))
                raise
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

    async def get_alarms(self, vin: str, *, page: int = 1, page_size: int = 20) -> dict:
        """One page of the car's alert history (GAC 2.0.32 ``alarm/page``, account API).

        The page has ``list`` items with ``alarmId``, ``vin``, ``createTime``, ``alarmType`` and
        ``alarmContent``: a JSON string holding ``alarmStatus[{alarmSource, alarms[{alarmObject,
        alarmReason}]}]``, which the app turns into a title (charging finished, a living being
        left in the car, 12 V battery low and so on).
        """
        body = {"pageNo": page, "pageSize": page_size, "order": "", "sort": "", "vin": vin}
        resp = await self._account_call("/icv/api/alarm/page/sec", body)
        data = self._data(resp)
        if not self._ok(resp) or not isinstance(data, dict):
            safe_code = _safe_result_code(resp.get("code") if isinstance(resp, dict) else None)
            if safe_code == ACCOUNT_SIGNED_IN_ELSEWHERE:
                raise AccountSessionEnded(f"alert list was not accepted: the GAC account signed in on another phone "
                                          f"(result code {safe_code}); a new sign-in is needed for it")
            if safe_code in ACCOUNT_SESSION_ENDED:
                raise AccountSessionEnded(f"alert list was not accepted: GAC's account session has ended and could "
                                          f"not be refreshed (result code {safe_code}); a new sign-in is needed for it")
            detail = f"result code {safe_code}" if safe_code is not None else "no recognized result code"
            raise ResponseError(f"alert list was not accepted ({detail})")
        return data

    async def get_departure_plans(self, vin: str, *, version: str | None = None) -> Any:
        """The car's saved departure plans (GAC 2.0.32 one-click setout, new generation).

        Each plan has ``customFeatureId``, ``configName``, ``leaveTime`` (seconds of the day),
        ``weeklyReservation``, ``active`` and ``content`` (climate, seats, steering wheel,
        battery temperature).
        """
        body = {"vin": vin, "tinyId": "", "type": "oks", **({"version": version} if version else {})}
        resp = await self._iov_read("/iov-custom-config/new/v1/user/oks/configs/list", body, sensitive={"vin": vin})
        data = self._data(resp)
        if not isinstance(resp, dict) or resp.get("success") is False or not isinstance(data, (dict, list)):
            safe_code = _safe_result_code(resp.get("code") if isinstance(resp, dict) else None)
            detail = f"result code {safe_code}" if safe_code is not None else "no valid data"
            raise ResponseError(f"departure plan list was not accepted ({detail})")
        return data

    async def get_standby_config(self, vin: str, *, default: bool = False) -> Any:
        """The older single "One-click standby" setting (GAC 2.0.32 ``user/oks/config/query``),
        or with ``default`` the default plan (``default/config/query``).

        Its reply has ``customFeatureId``, ``active``, ``type``, ``version`` and ``content``
        (cabin temperature control and seats).
        """
        path = f"/iov-custom-config/v1/{'default' if default else 'user/oks'}/config/query"
        resp = await self._iov_read(path, {"vin": vin, "type": "oks"}, sensitive={"vin": vin})
        if not isinstance(resp, dict) or resp.get("success") is False:
            safe_code = _safe_result_code(resp.get("code") if isinstance(resp, dict) else None)
            detail = f"result code {safe_code}" if safe_code is not None else "no valid data"
            raise ResponseError(f"standby setting was not accepted ({detail})")
        return self._data(resp)

    async def get_status(self, vin: str) -> VehicleStatus:
        resp = await self._iov_read(commands.CATALOG["status"].path, {"vin": vin}, sensitive={"vin": vin})
        if isinstance(resp, dict) and resp.get("success") is False:
            safe_code = _safe_result_code(resp.get("code"))
            raise ResponseError(f"vehicle status read was refused (result code {safe_code})")
        data = self._data(resp)
        reports = data.get("results") if isinstance(data, dict) else None
        if not isinstance(reports, list) or not reports or not isinstance(reports[0], dict):
            raise ResponseError("vehicle status response has no valid report")
        status = VehicleStatus.from_results(reports[0], online=data.get("online"), updated_ms=data.get("updateTime"))
        charging = reports[0].get("chargingAccessors")
        charging = charging[0] if isinstance(charging, list) and charging else None
        if (window := vehicle.reported_window(charging)):
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

    async def poll_command_result(self, vin: str, session_id: str) -> Any:
        """The car's answer to an accepted command, read from the gateway instead of the push feed.

        Returns None while the command is still executing (13001), else the answer as a
        ``PushResult``: the reply carries a push message's fields (identifier, updateTime,
        results), as the GAC 2.0.32 app reads it when push is unavailable.
        """
        # Push results and polled ones read alike.
        from .push import interpret, normalize_id

        resp = await self._iov_read(IOV_POLL_RESULT, {"identifier": {"sessionId": session_id, "vin": vin}},
                                    sensitive={"vin": vin})
        _LOGGER.debug("command result poll: %s", _excerpt(resp, 4000).replace(vin, "<VIN>"))
        code = resp.get("code") if isinstance(resp, dict) else None
        if type(code) in (int, str) and str(code) == str(CODE_COMMAND_ACCEPTED):
            return None
        result = interpret(resp if isinstance(resp, dict) else {})
        return dataclasses.replace(result, session_id=result.session_id or normalize_id(session_id),
                                   vin=result.vin or vin)

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
        _LOGGER.debug("command %s: %s", path.rsplit("/access_gateway/", 1)[-1],
                      _excerpt(body, 1000).replace(vin, "<VIN>"))
        resp = await self._iov_call(path, body, sensitive={"vin": vin}, kind="command")
        try:
            return _command_result(resp, path)
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
        fan_speed: int | None = None, blowing_mode: int | None = None, cycling_mode: int | None = None,
        compressor: bool = True,
    ) -> Any:
        """Start manual A/C, optionally selecting the blower, outlet or air-source code.

        Omitted fan_speed/blowing_mode/cycling_mode fields preserve the vehicle's settings.
        Air source (cyclingMode) 1 recirculates and 2 draws fresh air on an Aion UT,
        which applies it only while its A/C is already running.
        ``compressor=False`` runs the blower alone, the console's Fan mode; an Aion UT
        accepted it both from off and while its A/C ran.
        A selection starts or renews the timed run at the supplied temperature.
        Fan speed uses 1–10; outlet codes must be calibrated for the vehicle.
        Manual startup and fan writes were verified on an Aion UT. Outlet codes
        1–4 worked with that car fully on (ready to drive); 3 had no effect
        before then and 5 was rejected. Other codes/models remain unverified.
        """
        target, duration = commands.validate_climate(temperature, minutes)
        params = {"temperature": target, "currentDuration": duration}
        if not commands.validate_compressor(compressor):
            params["enableAirCompressor"] = 0   # the envelope's default 1 runs the compressor
        if fan_speed is not None:
            params["windStrength"] = commands.validate_fan_speed(fan_speed)
        if blowing_mode is not None:
            params["blowingMode"] = commands.validate_blowing_mode(blowing_mode)
        if cycling_mode is not None:
            params["cyclingMode"] = commands.validate_cycling_mode(cycling_mode)
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

    async def set_seat_ventilation_level(self, vin: str, seat: int, level: int) -> Any:
        """Set ventilation strength 1–3, or 0 for off (verified on an Aion UT's front seats)."""
        if type(seat) is not int or seat not in (1, 2, 3, 4):
            raise ValueError("seat must be an integer from 1 to 4")
        level = commands.validate_seat_level(level)
        if level == 0:
            return await self.command(vin, f"seat-{seat}-ventilation-off")
        return await self.command(vin, f"seat-{seat}-ventilation-on", windStrength=level)

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
        """Allow charging whenever plugged in; the car keeps its saved window for a later resume.

        This does not start a new charge once one has ended (``models.restart_needed``); use
        ``restart_charging`` first, then this once charging has begun.
        """
        return await self._reservation(vin, vehicle.charge_now_operation(until_soc=until_soc))

    async def restart_charging(self, vin: str) -> Any:
        """Start a new charge once one has ended: one time to the charge limit from the next minute.

        This replaces the saved window, so it is not meant for a repeating schedule.
        """
        return await self._reservation(vin, vehicle.restart_operation(self._zone()))

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

    async def set_charge_window(self, vin: str, start: str, stop: str | None, *,
                                weekly: int = 0, until_soc: bool = False) -> Any:
        """Schedule charging on the ``weekly`` days, or once (``weekly`` 0) for the next occurrence.

        Times are car-local in the scheduling zone. With ``until_soc`` the car stops at its
        charge limit instead; no stop time is sent, so ``stop`` may be None.
        Acceptance still needs vehicle confirmation.
        """
        op = vehicle.window_operation(start, stop, self._zone(), weekly=weekly, until_soc=until_soc)
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


def _command_result(resp: Any, path: str = "") -> Any:
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
        hint = commands.refusal_hint(code, path)
        raise CommandError(f"command rejected: {detail}" + (f". {hint}" if hint else ""), code=code)
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
