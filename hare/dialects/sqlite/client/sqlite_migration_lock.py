from __future__ import annotations

import asyncio
import importlib
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, BinaryIO, ClassVar

from hare.dialects.sqlite.client.constants import (
    SQLITE_MIGRATION_LOCK_POLL_SECONDS,
    SQLITE_MIGRATION_LOCK_SUFFIX,
    WINDOWS_OS_NAME,
)


class SqliteMigrationLock:
    """The lock ``migrate`` holds for its whole run on a SQLite database file: an OS lock on the file
    ``<database>-migrate-lock`` beside it, given back with the file's handle - also when the process
    dies. SQLite's own write lock can't serve: held on another connection, it would keep the
    migrations themselves out."""

    #: The OS's locking of files - ``msvcrt`` on Windows, ``fcntl`` elsewhere.
    file_locking: ClassVar[Any] = importlib.import_module("msvcrt" if os.name == WINDOWS_OS_NAME else "fcntl")

    @classmethod
    @asynccontextmanager
    async def hold(cls, database_path: str) -> AsyncGenerator[None]:
        """Holds the lock of a database file, waiting while another process holds it.

        Args:
            database_path: The database file.
        """
        with open(database_path + SQLITE_MIGRATION_LOCK_SUFFIX, "a+b") as lock_file:
            while not cls.try_lock(lock_file):
                await asyncio.sleep(SQLITE_MIGRATION_LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                cls.unlock(lock_file)

    @classmethod
    def try_lock(cls, lock_file: BinaryIO) -> bool:
        """Takes the lock of an open lock file, if no other process holds it.

        Args:
            lock_file: The file.

        Returns:
            Whether it was taken.
        """
        file_locking = cls.file_locking
        try:
            if os.name == WINDOWS_OS_NAME:
                lock_file.seek(0)
                file_locking.locking(lock_file.fileno(), file_locking.LK_NBLCK, 1)
            else:
                file_locking.flock(lock_file.fileno(), file_locking.LOCK_EX | file_locking.LOCK_NB)
        except OSError:
            # Held by another process - BlockingIOError on POSIX, PermissionError on Windows.
            return False
        return True

    @classmethod
    def unlock(cls, lock_file: BinaryIO) -> None:
        """Gives the lock of an open lock file back.

        Args:
            lock_file: The file.
        """
        file_locking = cls.file_locking
        if os.name == WINDOWS_OS_NAME:
            lock_file.seek(0)
            file_locking.locking(lock_file.fileno(), file_locking.LK_UNLCK, 1)
        else:
            file_locking.flock(lock_file.fileno(), file_locking.LOCK_UN)
