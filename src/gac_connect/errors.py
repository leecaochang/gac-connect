"""Exception hierarchy, mapped from gateway result codes."""
from __future__ import annotations

import re


class GacError(Exception):
    """Base for every error raised by this library."""


class ResponseError(GacError):
    """An HTTP or response-shape failure, not evidence of expired credentials."""


class AccountSessionEnded(ResponseError):
    """GAC's account session has ended (the app signs in elsewhere and GAC allows one).

    Only account-API features, such as the alert list, need it; the vehicle session it
    minted keeps working, so this is not a reason to sign everything in again."""


class AuthError(GacError):
    """Sign-in could not be completed."""


class CaptchaError(AuthError):
    """The block-puzzle step failed (wrong slide, or a bad ticket)."""


class LoginError(AuthError):
    """Credentials or the one-time code were rejected."""


class AuthExpiredError(AuthError):
    """The refresh token is spent or invalid; a fresh interactive login is needed.

    In Home Assistant this maps to a reauth flow."""


class TokenInvalidError(GacError):
    """The access token was rejected; callers should refresh once and retry.

    Handled internally by the client; rarely surfaces."""


class RateLimitedError(GacError):
    """The gateway asked us to slow down. `retry_after` is seconds, if known."""

    def __init__(self, message: str = "", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RepeatRequestError(GacError):
    """The gateway rejected a duplicate request; retry with a fresh request id."""


# Command result codes named by the GAC 2.0.32 app (wording is this library's own).
# 13000 executed and 13001 executing are successes; 13007 (partial success) is left unnamed.
RESULT_REASONS: dict[str, str] = {
    "11018": "the remote-control PIN is incorrect",
    "13002": "not supported by this vehicle",
    "13003": "not allowed in the vehicle's current state",
    "13004": "a previous command is still in progress",
    "13005": "the vehicle's conditions for this command are not met",
    "13006": "the vehicle's conditions for this command are not met",
    "13008": "the vehicle is offline",
    "13101": "the command is invalid",
    "13404": "the departure plan could not be configured",
    "13414": "a matching schedule already exists",
    "13426": "too many schedules (the vehicle allows 7)",
    "13902": "the vehicle did not respond in time",
    "13903": "the vehicle is not powered on",
    "13904": "the vehicle could not be reached",
    "13905": "the vehicle's voltage is abnormal",
    "13906": "the command is invalid",
    "13907": "it already exists",
    "15001": "another command is executing",
    "15024": "the vehicle is not stationary",
    "15030": "a door or the tailgate is open",
    "15033": "the hazard lights are on",
    "15034": "a window is open",
    "15042": "the vehicle is not in Park",
    "15043": "the fridge door is open",
}
# 15016–15045 are detailed refusals in the app, named above where known.
REFUSAL_CODES = frozenset({*RESULT_REASONS, "13102", "13103", *(str(code) for code in range(15016, 15046))})


def result_reason(code: object) -> str | None:
    """A short explanation for a known command result code, else None."""
    return RESULT_REASONS.get(str(code)) if type(code) in (int, str) else None


class CommandError(GacError):
    """A vehicle command was rejected or malformed."""

    def __init__(self, message: str = "", *, code: object = None) -> None:
        super().__init__(message)
        # Diagnostics may retain this bounded service code, never the API message.
        value = str(code) if type(code) in (int, str) else ""
        self.code = value if re.fullmatch(r"(?:[0-9]{1,8}|SDK\.[A-Z]{1,12}\.[0-9]{1,8})", value) else None
        self.reason = result_reason(self.code)


class PinRequiredError(CommandError):
    """This command needs the remote-control PIN, which is not yet supported."""


class RegionError(GacError):
    """Unknown or unsupported region code."""
