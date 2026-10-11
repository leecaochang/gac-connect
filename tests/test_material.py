from unittest.mock import Mock

import pytest
from gac_connect import GacClient
from gac_connect.crypto import decrypt_envelope, encrypt_envelope
from gac_connect.keys import Material, load_material


@pytest.mark.parametrize("mode", ["legacy", "explicit", "loaded"])
def test_injected_material_decrypts_gateway_and_push_responses(mode):
    base = load_material()
    response_pair = base.main if mode == "explicit" else base.iov
    kwargs = {"iov_response": response_pair} if mode == "explicit" else {}
    material = base if mode == "loaded" else Material(
        base.iov, base.main, base.iov_hmac, base.main_hmac, base.captcha_key, **kwargs,
    )
    assert material.iov_response is response_pair
    client = GacClient("AU", Mock(), material=material)
    message = {"success": True, "data": {"synthetic": 1}}
    envelope, _, _ = encrypt_envelope(message, response_pair.public)
    assert decrypt_envelope(envelope, client._response_key(client._cfg["iov"])) == message
    assert client.decrypt_push(envelope) == message
    assert client._response_key(client._cfg["main"]) is base.main.private
