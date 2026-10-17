import asyncio
import json
import os
import threading
from unittest.mock import patch

import pytest

from gac_connect.session import FileStore, Session


def test_atomic_save_is_private_and_roundtrips(tmp_path):
    store = FileStore(tmp_path / "session.json")
    original = os.replace
    seen = []

    def replace(src, dst):
        seen.append(os.stat(src).st_mode & 0o777)
        original(src, dst)

    async def run():
        with patch("gac_connect.session.os.replace", replace):
            await store.save(Session(token="test", refresh_token="new"))
        assert (await FileStore(store._path).load()).refresh_token == "new"

    asyncio.run(run())
    assert seen == [0o600]
    assert store._path.stat().st_mode & 0o777 == 0o600


def test_failed_replace_keeps_previous_file_and_cleans_temporary(tmp_path):
    store = FileStore(tmp_path / "session.json")

    async def run():
        await store.save(Session(token="previous"))
        with patch("gac_connect.session.os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                await store.save(Session(token="new"))
        assert (await store.load()).token == "previous"

    asyncio.run(run())
    # No temporary file is left; the empty .lock beside it is the store's lock file.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["session.json", "session.json.lock"]


def test_cancelled_save_finishes_off_loop_before_returning(tmp_path, monkeypatch):
    store = FileStore(tmp_path / "session.json")
    entered, release = threading.Event(), threading.Event()
    original = store._save
    main_thread = threading.get_ident()

    def slow(data):
        assert threading.get_ident() != main_thread
        entered.set()
        assert release.wait(5)
        original(data)

    monkeypatch.setattr(store, "_save", slow)

    async def run():
        task = asyncio.create_task(store.save(Session(token="new")))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert json.loads(store._path.read_text())["token"] == "new"

    asyncio.run(run())
