"""GAC Connect — an unofficial client for GAC / Aion international connected-car accounts.

Not affiliated with, endorsed by, or supported by GAC. For use with a vehicle you
own, on your own account.
"""
from __future__ import annotations

from .client import command_session_id
from .errors import (
    AuthError,
    AuthExpiredError,
    CaptchaError,
    CommandError,
    GacError,
    LoginError,
    PinRequiredError,
    RateLimitedError,
    RegionError,
)
from .limits import DEFAULT_LIMITER, Limiter
from .push import BrokerInfo, PushClient, PushResult, interpret

__version__ = "0.4.0b6"

__all__ = [
    "AuthError",
    "DEFAULT_LIMITER",
    "Limiter",
    "BrokerInfo",
    "PushClient",
    "PushResult",
    "interpret",
    "AuthExpiredError",
    "CaptchaError",
    "CommandError",
    "GacError",
    "LoginError",
    "PinRequiredError",
    "RateLimitedError",
    "RegionError",
    "__version__",
    "command_session_id",
]
