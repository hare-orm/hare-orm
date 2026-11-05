from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path


class SqliteDatabase:
    """The throwaway SQLite file a run creates and deletes afterwards - in the temporary directory, on
    the disk every ORM writes to alike."""

    def __init__(self, name: str) -> None:
        """
        Args:
            name: The file's name, without its directory and suffix.
        """
        self.name = name
        self.path = Path(tempfile.gettempdir()) / f"{name}.sqlite3"
        self.created = False

    def remove_files(self) -> None:
        """Removes the file and its journal files."""
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)

    async def create(self) -> str:
        """Starts from no file.

        Returns:
            The version of the SQLite library this Python links.
        """
        self.remove_files()
        self.created = True
        return sqlite3.sqlite_version

    async def settle(self) -> None:
        """Nothing to do: SQLite has no background vacuum to come at a random moment of a run."""

    async def drop(self) -> None:
        """Remove the database files, passing cleanup failures to the result collector."""
        self.remove_files()
        self.created = False
