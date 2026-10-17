"""Session state and its persistence contract.

A session holds the IoV tokens used to read and command the vehicle, plus the
main-API token used only during sign-in. The refresh token is single-use: every
refresh rotates it, so the new pair must be persisted *before* the next request.
The client calls ``TokenStore.save`` on every rotation; supply a store that
writes wherever your host keeps state (a config entry, a JSON file, memory).
"""
from __future__ import annotations

import asyncio

try:
    import fcntl
except ImportError:     # not POSIX: in-process turns only
    fcntl = None
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, replace
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


def credential_pair(session: Session) -> tuple[str | None, str | None]:
    """What identifies a saved session: its vehicle-session token pair."""
    return session.token, session.refresh_token


class ConditionalStore(TokenStore, Protocol):
    async def replace(self, expected: tuple[str | None, str | None], session: Session) -> Session | None:
        """Save ``session`` only if the stored pair is still ``expected``, as one step.

        Returns None once saved, or the stored session, unchanged, if something else (a new
        sign-in) replaced the expected one. An unreadable stored session counts as expected:
        the session being saved is then the only usable one.
        """


class MemoryStore:
    """A store that keeps the session only in memory (default)."""

    def __init__(self, session: Session | None = None) -> None:
        self._session = replace(session) if session else Session()

    # Copies in and out, as a file would: a caller changing its session never changes what is stored.
    async def load(self) -> Session:
        return replace(self._session)

    async def save(self, session: Session) -> None:
        self._session = replace(session)

    async def replace(self, expected: tuple[str | None, str | None], session: Session) -> Session | None:
        if credential_pair(self._session) != expected:
            return replace(self._session)
        self._session = replace(session)
        return None


class FileStore:
    """A store backed by a JSON file (used by the CLI). Written 0600."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()
        self._lock = asyncio.Lock()

    async def load(self) -> Session:
        return await asyncio.to_thread(self._load)

    def _load(self) -> Session:
        try:
            return Session.from_dict(json.loads(self._path.read_text()))
        except FileNotFoundError:
            return Session()

    async def save(self, session: Session) -> None:
        async with self._lock:
            await _finish(asyncio.to_thread(self._locked, self._save, session.to_dict()))

    async def replace(self, expected: tuple[str | None, str | None], session: Session) -> Session | None:
        async with self._lock:
            return await _finish(asyncio.to_thread(self._locked, self._replace, expected, session))

    def _locked(self, work, *args):
        """Hold an exclusive lock on the file (a sidecar ``.lock``) for one read-compare-write,
        so separate store objects and separate processes on the same file take turns too."""
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(self._path.with_name(self._path.name + ".lock"), "a+") as lock:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                return work(*args)
            finally:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _replace(self, expected: tuple[str | None, str | None], session: Session) -> Session | None:
        try:
            current = self._load()
        except (OSError, ValueError):
            current = None              # unreadable: the session being saved is the usable one
        if current is not None and credential_pair(current) != expected:
            return current
        self._save(session.to_dict())
        return None

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


async def _finish(work):
    """Await a disk operation to its end, even when the caller is cancelled meanwhile."""
    task = asyncio.ensure_future(work)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Do not release a caller's refresh gate while its disk write still runs.
        await task
        raise
