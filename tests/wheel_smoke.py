"""Run directly with an isolated interpreter after installing the built wheel."""
import asyncio
import sys
from importlib.resources import files
from pathlib import Path

import aiohttp
import gac_connect
from gac_connect import GacClient
from gac_connect.crypto import decrypt_envelope, encrypt_envelope
from gac_connect.keys import load_material


async def main():
    assert Path(gac_connect.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    assert files(gac_connect).joinpath("py.typed").is_file()
    for region, bundle in [("AU", "_material.pem"), ("GB", "_material_uk.pem")]:
        material = load_material(bundle)
        response = material.iov_response or material.iov
        message = {"success": True, "data": {"synthetic": 1}}
        envelope, _, _ = encrypt_envelope(message, response.public)
        assert decrypt_envelope(envelope, response.private) == message
        async with aiohttp.ClientSession() as http:
            client = GacClient(region, http, material=material)
            assert client.decrypt_push(envelope) == message
    print("Installed wheel imports, bundled keys, typing marker and decryption passed")


if __name__ == "__main__":
    asyncio.run(main())
