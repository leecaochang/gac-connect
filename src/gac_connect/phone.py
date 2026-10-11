"""Normalize an explicit international prefix using the selected region."""
from __future__ import annotations

import re

from .errors import LoginError


def local_mobile(value: str, country_tel: str) -> str:
    if not isinstance(value, str):
        raise LoginError("Enter a local phone number or an explicit international number")
    compact = re.sub(r"[\s()-]", "", value)
    if re.fullmatch(r"(?:\+|00)?[0-9]+", compact) is None:
        raise LoginError("Enter a local phone number or an explicit international number")
    digits = compact
    if compact.startswith(("+", "00")):
        digits = compact[1:] if compact.startswith("+") else compact[2:]
        prefix = country_tel.lstrip("+")
        if not digits.startswith(prefix):
            raise LoginError("The phone country code does not match the selected region")
        digits = digits[len(prefix):]
    digits = digits.lstrip("0")
    if not digits:
        raise LoginError("The phone number is empty")
    return digits
