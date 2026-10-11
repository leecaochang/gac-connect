"""Session state and its persistence contract.

A session holds the IoV tokens used to read and command the vehicle, plus the
main-API token used only during sign-in. The refresh token is single-use: every
refresh rotates it, so the new pair must be persisted *before* the next request.
The client calls ``TokenStore.save`` on every rotation; supply a store that
writes wherever your host keeps state (a config entry, a JSON file, memory).
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class Session:
    # IoV gateway tokens (read + command the car)
    token: str | None = None
    refresh_token: str | None = None
    expire_time: int | None = None       # ms epoch
    rexpire_time: int | None = None      # ms epoch
    # main-API token (sign-in only; carries its own refresh pair)
    main_token: str | None = None
    main_refresh_token: str | None = None
    region: str = "AU"
    vin: str | None = None
    # set when the session can no longer be refreshed; cleared by a new sign-in
    expired: bool = False

    @property
    def access_valid(self) -> bool:
        return bool(self.token) and (self.expire_time or 0) > int(time.time() * 1000) + 5000

    @property
    def refresh_valid(self) -> bool:
        return bool(self.refresh_token) and (self.rexpire_time or 0) > int(time.time() * 1000)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> Session:
        if not data:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@runtime_checkable
class TokenStore(Protocol):
    async def load(self) -> Session: ...
    async def save(self, session: Session) -> None: ...


class MemoryStore:
    """A store that keeps the session only in memory (default)."""

    def __init__(self, session: Session | None = None) -> None:
        self._session = session or Session()

    async def load(self) -> Session:
        return self._session

    async def save(self, session: Session) -> None:
        self._session = session


class FileStore:
    """A store backed by a JSON file (used by the CLI). Written 0600."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()

    async def load(self) -> Session:
        return await asyncio.to_thread(self._load)

    def _load(self) -> Session:
        try:
            return Session.from_dict(json.loads(self._path.read_text()))
        except FileNotFoundError:
            return Session()

    async def save(self, session: Session) -> None:
        task = asyncio.create_task(asyncio.to_thread(self._save, session.to_dict()))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Do not release a caller's refresh gate while its disk write still runs.
            await task
            raise

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix=f".{self._path.name}.", dir=self._path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            if os.name == "posix":
                directory = os.open(self._path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
