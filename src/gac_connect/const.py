"""Endpoints, regions and protocol constants for the GAC international API."""
from __future__ import annotations

from datetime import timedelta, timezone
from typing import Final

# Main account API and the IoV vehicle gateway are two separate backends with
# two separate crypto profiles.
IOV_PREFIX: Final = "/iov-vehicle-gateway/v1/platform/vehicle/access_gateway"
# The app's way to read a command result without the push feed (GAC 2.0.32); note /iov/, not /platform/.
IOV_POLL_RESULT: Final = "/iov-vehicle-gateway/v1/iov/vehicle/access_gateway/poll_access_response"

# Both hosts are built from a per-country prefix, so the table holds prefixes
# instead of repeating two long names 56 times. Each country has a main API prefix
# and an IoV gateway prefix (the IoV default is "sea"). The one irregular host is
# the "sea" gateway: it exists only as sea-public-iov-sdk-access, never as bare
# sea-iov-sdk-access.
_MAIN_HOST: Final = "{}-app-api.gac-international.com"
_IOV_HOST: Final = "{}-iov-sdk-access.gac-international.com"

# country -> (main API prefix, IoV gateway prefix, calling code, time zone).
# One time zone per country, which is only an opening guess where a country spans
# several (RU, BR, MX, ID, AU, ES, PT, CL); pass ``timezone=`` to GacClient to
# schedule against the one the car is actually parked in.
_TABLE: Final[dict[str, tuple[str, str, str, str]]] = {
    "AE": ("sa", "me", "+971", "Asia/Dubai"),
    "AT": ("nl", "eu", "+43", "Europe/Vienna"),
    "AU": ("nl", "eu", "+61", "Australia/Brisbane"),
    "BE": ("nl", "eu", "+32", "Europe/Brussels"),
    "BG": ("nl", "eu", "+359", "Europe/Sofia"),
    "BH": ("sa", "me", "+973", "Asia/Bahrain"),
    "BR": ("br", "sa", "+55", "America/Sao_Paulo"),
    "CH": ("nl", "eu", "+41", "Europe/Zurich"),
    "CL": ("br", "sa", "+56", "America/Santiago"),
    "CZ": ("nl", "eu", "+420", "Europe/Prague"),
    "DE": ("nl", "eu", "+49", "Europe/Berlin"),
    "DK": ("nl", "eu", "+45", "Europe/Copenhagen"),
    "ES": ("nl", "eu", "+34", "Europe/Madrid"),
    "FI": ("nl", "eu", "+358", "Europe/Helsinki"),
    "FR": ("nl", "eu", "+33", "Europe/Paris"),
    "GB": ("nl", "eu", "+44", "Europe/London"),
    "GR": ("nl", "eu", "+30", "Europe/Athens"),
    "HK": ("sg", "sea", "+852", "Asia/Hong_Kong"),
    "HR": ("nl", "eu", "+385", "Europe/Zagreb"),
    "HU": ("nl", "eu", "+36", "Europe/Budapest"),
    "ID": ("sg", "sea", "+62", "Asia/Jakarta"),
    "IE": ("nl", "eu", "+353", "Europe/Dublin"),
    "IL": ("nl", "eu", "+972", "Asia/Jerusalem"),
    "IQ": ("sa", "me", "+964", "Asia/Baghdad"),
    "IT": ("nl", "eu", "+39", "Europe/Rome"),
    "JO": ("sa", "me", "+962", "Asia/Amman"),
    "KH": ("sg", "sea", "+855", "Asia/Phnom_Penh"),
    "KW": ("sa", "me", "+965", "Asia/Kuwait"),
    "LA": ("sg", "sea", "+856", "Asia/Vientiane"),
    "LB": ("sa", "me", "+961", "Asia/Beirut"),
    "MM": ("sg", "sea", "+95", "Asia/Yangon"),
    "MO": ("sg", "sea", "+853", "Asia/Macau"),
    "MX": ("br", "sa", "+52", "America/Mexico_City"),
    "MY": ("sg", "sea", "+60", "Asia/Kuala_Lumpur"),
    "NL": ("nl", "eu", "+31", "Europe/Amsterdam"),
    "NO": ("nl", "eu", "+47", "Europe/Oslo"),
    "NP": ("sg", "sea", "+977", "Asia/Kathmandu"),
    "NZ": ("nl", "eu", "+64", "Pacific/Auckland"),
    "OM": ("sa", "me", "+968", "Asia/Muscat"),
    "PA": ("br", "sa", "+507", "America/Panama"),
    "PH": ("sg", "sea", "+63", "Asia/Manila"),
    "PK": ("sa", "me", "+92", "Asia/Karachi"),
    "PL": ("nl", "eu", "+48", "Europe/Warsaw"),
    "PT": ("nl", "eu", "+351", "Europe/Lisbon"),
    "QA": ("sa", "me", "+974", "Asia/Qatar"),
    "RO": ("nl", "eu", "+40", "Europe/Bucharest"),
    "RU": ("ru", "ru", "+7", "Europe/Moscow"),
    "SA": ("sa", "me", "+966", "Asia/Riyadh"),
    "SE": ("nl", "eu", "+46", "Europe/Stockholm"),
    "SG": ("sg", "sea", "+65", "Asia/Singapore"),
    "SI": ("nl", "eu", "+386", "Europe/Ljubljana"),
    "SK": ("nl", "eu", "+421", "Europe/Bratislava"),
    "TH": ("sg", "sea", "+66", "Asia/Bangkok"),
    "TR": ("nl", "eu", "+90", "Europe/Istanbul"),
    "VN": ("sg", "sea", "+84", "Asia/Ho_Chi_Minh"),
    "ZA": ("sa", "me", "+27", "Africa/Johannesburg"),
}

# Countries the app offers in its own country picker: markets that have launched,
# so an account can exist to sign in with. The rest are wired up in the build
# ahead of sale and may answer nothing useful yet.
LISTED_REGIONS: Final = frozenset({
    "AE", "AU", "BR", "ES", "FI", "GR", "HK", "ID", "IL", "KH", "KW",
    "MO", "MX", "NZ", "PH", "PL", "PT", "RU", "SA", "SG", "TH",
})

# Regions this library has been signed in and driven a car with. Everything else
# is listed but untested — reports welcome.
CONFIRMED_REGIONS: Final = frozenset({"AU", "NZ"})

# A "profile" is a separate national app: its own backend, its own key material,
# its own main host. Same protocol and endpoints as GAC International, so only the
# material bundle and the main host differ. The IoV gateway is shared. The default
# profile is GAC International itself. My AION (UK) is the first separate one; its
# material is in its own bundle (see PROFILE_MATERIAL), shipped with the package
# like the default one.
PROFILE_MATERIAL: Final[dict[str, str]] = {
    "intl": "_material.pem",
    "uk": "_material_uk.pem",
}
# region -> profile (anything unlisted is GAC International).
REGION_PROFILE: Final[dict[str, str]] = {"GB": "uk"}
# region -> a main-API host that is not the "<prefix>-app-api.gac-international.com"
# pattern (a separate national app's own host).
_MAIN_HOST_OVERRIDE: Final[dict[str, str]] = {"GB": "app-api.aionauto.co.uk"}

REGIONS: Final[dict[str, dict[str, str]]] = {
    code: {
        "main": _MAIN_HOST_OVERRIDE.get(code) or _MAIN_HOST.format(main),
        "iov": _IOV_HOST.format("sea-public" if iov == "sea" else iov),
        "tel": tel,
        "tz": tz,
        "listed": code in LISTED_REGIONS,
        "supported": code in CONFIRMED_REGIONS,
        "profile": REGION_PROFILE.get(code, "intl"),
    }
    for code, (main, iov, tel, tz) in _TABLE.items()
}
DEFAULT_REGION: Final = "AU"

# Header identity the gateway expects (Android client profile).
APP_VERSION: Final = "2.0.25"
MAIN_APP_ID: Final = "app-android"
IOV_APP_ID: Final = "2"
# IoV subsystem app id, per profile. GAC International runs the IoV subsystem
# under "2"; a separate national app may run it under its own id. My AION (UK)
# issues its IoV session under appId "3" (seen in the real app's IoV session
# JWT), and exchanging a UK ticket under "2" is rejected with "ticket check
# fail". Anything unlisted falls back to IOV_APP_ID.
PROFILE_IOV_APP_ID: Final[dict[str, str]] = {"intl": "2", "uk": "3"}
# AES key size (bytes) for an IoV request body, per profile. GAC International
# uses a 32-byte key (AES-256); My AION (UK) seals its IoV requests with a
# 24-byte key (AES-192) instead, even though its IoV responses stay AES-256.
# Anything unlisted falls back to 32.
PROFILE_IOV_REQUEST_KEY_SIZE: Final[dict[str, int]] = {"uk": 24}
IOV_VERSION: Final = "v1"
USER_AGENT: Final = "Dart/3.7 (dart:io)"

# SSO service that mints the IoV ticket from a main-API session.
SSO_SERVICE_ID: Final = "AIA-TSP"

# Captcha canvas geometry (anji-plus block puzzle).
PUZZLE_WIDTH: Final = 310
PUZZLE_HEIGHT: Final = 155
PUZZLE_PIECE_WIDTH: Final = 47
PUZZLE_MAX_X: Final = PUZZLE_WIDTH - PUZZLE_PIECE_WIDTH  # slider travel, 0..263

# Timeouts (seconds).
HTTP_TIMEOUT: Final = 20.0
# The gateway refreshes vehicle status about every 30 s; polling faster is wasteful.
MIN_POLL_INTERVAL: Final = 30.0
# Hard floor between ANY two gateway requests, serialized in the client. Normal
# polling is minutes apart so this never bites; it only caps bursts/misuse.
MIN_REQUEST_GAP: Final = 1.0
# Rolling budgets per client, as (window seconds, most requests in that window).
# Every gateway request counts, whatever triggered it (polls, refreshes, sign-in,
# push-channel details, commands). Past a limit the client refuses locally with
# RateLimitedError and sends nothing until the window frees up. Normal use (a poll
# every few minutes plus the odd command) stays far below all of them.
REQUEST_BUDGETS: Final = ((60, 20), (3600, 240), (86400, 3000))
# Vehicle commands reach the car itself, so they get their own, tighter budgets.
COMMAND_BUDGETS: Final = ((60, 6), (3600, 60))
# After the gateway answers 429, nothing is sent for this long, or for its
# Retry-After if that is longer (capped at MAX_RATE_LIMIT_COOLDOWN, one day).
RATE_LIMIT_COOLDOWN: Final = 60.0
MAX_RATE_LIMIT_COOLDOWN: Final = 86400.0
# If a saved request-limit record is corrupt, any pause in it is unknown: pause as
# long as the longest pause the service could have asked for.
RECOVERY_PAUSE: Final = MAX_RATE_LIMIT_COOLDOWN

# The clock the service uses for charge-reservation timestamps: on the Singapore
# gateway an Aion UT in Asia/Bangkok reports a once window at 22:06 as 22:06+08:00.
# Other gateways are unverified; decoding prefers the calendar fields anyway.
SERVICE_CLOCK: Final = timezone(timedelta(hours=8))

# Gateway result codes.
CODE_OK: Final = "0000"
CODE_COMMAND_ACCEPTED: Final = 13001      # async command queued ("ongoing") — success
# The app's common response-success predicate (GAC 2.0.32) accepts these codes.
SUCCESS_RESULT_CODES: Final = frozenset({"0", "0000", "1000", "2000", "13000"})
CODE_COMMAND_MALFORMED: Final = 13101
CODE_UNSUPPORTED_VALUE: Final = 13002     # "not supported", e.g. an Aion UT refusing remote fan speeds 8–10
ERR_TOKEN_INVALID: Final = "SDK.GATE.0007"
ERR_VIN_HEADER: Final = "SDK.GATE.0017"
ERR_REPEAT_REQUEST: Final = "SDK.GATE.0018"
ERR_REFRESH_SPENT: Final = "ACCOUNT.0015"
ERR_CAPTCHA_WRONG: Final = 6111           # slider position wrong
ERR_CAPTCHA_TICKET: Final = 6110          # bad/expired captchaTicket
ERR_SERVICE_ID: Final = 1001005001        # unsupported serviceId
