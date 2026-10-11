"""Exception hierarchy, mapped from gateway result codes."""
from __future__ import annotations

import re


class GacError(Exception):
    """Base for every error raised by this library."""


class ResponseError(GacError):
    """An HTTP or response-shape failure, not evidence of expired credentials."""


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


class CommandError(GacError):
    """A vehicle command was rejected or malformed."""

    def __init__(self, message: str = "", *, code: object = None) -> None:
        super().__init__(message)
        # Diagnostics may retain this bounded service code, never the API message.
        value = str(code) if type(code) in (int, str) else ""
        self.code = value if re.fullmatch(r"(?:[0-9]{1,8}|SDK\.[A-Z]{1,12}\.[0-9]{1,8})", value) else None


class PinRequiredError(CommandError):
    """This command needs the remote-control PIN, which is not yet supported."""


class RegionError(GacError):
    """Unknown or unsupported region code."""
