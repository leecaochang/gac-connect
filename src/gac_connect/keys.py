"""Loads the material bundle used to sign and encrypt gateway traffic.

Values live in ``_material.pem`` alongside this module: two labelled PEM private
keys (``# @iov``, ``# @main``) and three ``# name: value`` constants
(``iov_hmac``, ``main_hmac``, ``captcha_key``). Public halves are derived at
import time. A missing bundle raises immediately with a clear message.

An IoV request and the response to it are sealed to the *same* keypair
(``iov_response``, falling back to ``iov`` when a profile does not ship its own
``@iov_response`` block); only the AES key size used for the request may differ
by profile (see ``PROFILE_IOV_REQUEST_KEY_SIZE`` in ``const.py``), independently
of the keypair. ``iov_hmac`` signs the request and is unrelated to either key.

A non-default bundle (a separate national app's profile) may omit ``@iov``
and/or ``iov_hmac`` independently: each one missing is inherited from the
default bundle. My AION (UK) ships its own material throughout: its own
main-API keypair and ``main_hmac``, its own IoV signing secret (``iov_hmac``),
and its own IoV keypair, shipped as ``@iov_response`` since that is the one its
IoV traffic, in both directions, actually uses. It shares only the captcha key
and the IoV gateway host with GAC International. The IoV gateway validates the
signature per app id, and the UK IoV subsystem runs under its own app id (see
``PROFILE_IOV_APP_ID`` in ``const.py``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from importlib import resources

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

_BUNDLE = "_material.pem"

# "# @name" marker, then everything up to the next marker or a metadata "# x:" line.
_BLOCK = re.compile(
    r"#\s*@(?P<name>\w+)\s*\n(?P<pem>-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----)",
    re.S,
)
_META = re.compile(r"^#\s*(\w+):\s*(.+?)\s*$", re.M)


@dataclass(frozen=True)
class Keypair:
    private: RSAPrivateKey
    public: RSAPublicKey

    @property
    def public_pem(self) -> bytes:
        return self.public.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )


@dataclass(frozen=True)
class Material:
    iov: Keypair
    main: Keypair
    iov_hmac: bytes
    main_hmac: bytes
    captcha_key: bytes
    # Keypair IoV requests are sealed to and IoV responses (and push messages)
    # are opened with. Usually the same object as ``iov``; a profile whose IoV
    # traffic uses a different keypair ships its own ``@iov_response`` block.
    iov_response: Keypair | None = None

    def __post_init__(self) -> None:
        # Preserve the five-argument constructor used before split response keys.
        if self.iov_response is None:
            object.__setattr__(self, "iov_response", self.iov)


def _load_private(pem_block: str) -> Keypair:
    key = serialization.load_pem_private_key(pem_block.encode(), password=None)
    if not isinstance(key, RSAPrivateKey):
        raise TypeError("expected an RSA private key in the material bundle")
    return Keypair(private=key, public=key.public_key())


@cache
def load_material(bundle: str = _BUNDLE) -> Material:
    try:
        text = resources.files(__package__).joinpath(bundle).read_text()
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            f"{bundle} is missing from the gac_connect package; the library cannot "
            "reach that backend without it."
        ) from exc

    blocks = {m.group("name").strip().lower(): m.group("pem") for m in _BLOCK.finditer(text)}
    meta = {k.lower(): v for k, v in _META.findall(text)}
    missing = [n for n in ("main",) if n not in blocks] + [
        n for n in ("main_hmac", "captcha_key") if n not in meta
    ]
    if missing:
        raise RuntimeError(f"{bundle} is incomplete; missing: {', '.join(missing)}")

    if bundle == _BUNDLE:
        if "iov" not in blocks or "iov_hmac" not in meta:
            raise RuntimeError(f"{bundle} is incomplete; missing: iov, iov_hmac")
        iov, iov_hmac = _load_private(blocks["iov"]), meta["iov_hmac"].encode()
    else:
        # Each half may be overridden independently; whichever is absent is
        # inherited from the default bundle. See the module docstring: My AION
        # (UK) ships its own iov keypair and HMAC secret.
        base = load_material(_BUNDLE)
        iov = _load_private(blocks["iov"]) if "iov" in blocks else base.iov
        iov_hmac = meta["iov_hmac"].encode() if "iov_hmac" in meta else base.iov_hmac

    # IoV requests seal to, and responses (and push messages) decrypt with,
    # ``iov`` unless the bundle ships a separate ``@iov_response`` keypair.
    iov_response = _load_private(blocks["iov_response"]) if "iov_response" in blocks else iov

    return Material(
        iov=iov,
        main=_load_private(blocks["main"]),
        iov_hmac=iov_hmac,
        main_hmac=meta["main_hmac"].encode(),
        captcha_key=meta["captcha_key"].encode(),
        iov_response=iov_response,
    )
