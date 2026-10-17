"""GAC Connect — an unofficial client for GAC / Aion international connected-car accounts.

Not affiliated with, endorsed by, or supported by GAC. For use with a vehicle you
own, on your own account.
"""
from __future__ import annotations

from .capabilities import Capabilities, Support, equipment_from_capabilities, ranges_from_capabilities
from .client import GacClient, command_session_id
from .errors import (
    AccountSessionEnded,
    AuthError,
    AuthExpiredError,
    CaptchaError,
    CommandError,
    GacError,
    LoginError,
    PinRequiredError,
    RateLimitedError,
    RegionError,
    ResponseError,
)
from .limits import DEFAULT_LIMITER, Limiter
from .models import VehicleStatus, doors_locked, restart_needed
from .phone import local_mobile
from .push import BrokerInfo, PushClient, PushResult, interpret
from .vehicle import validate_clock, validate_weekly, window_operation

# Public decoding entry point for consumers; use the same implementation as
# GacClient.get_status, without a second normalization pass or client subclass.
decode_status = VehicleStatus.from_results

__version__ = "0.5.0"

__all__ = [
    "GacClient",
    "Capabilities",
    "Support",
    "equipment_from_capabilities",
    "ranges_from_capabilities",
    "VehicleStatus",
    "decode_status",
    "doors_locked",
    "restart_needed",
    "local_mobile",
    "validate_clock",
    "validate_weekly",
    "window_operation",
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
    "ResponseError",
    "AccountSessionEnded",
    "__version__",
    "command_session_id",
]
