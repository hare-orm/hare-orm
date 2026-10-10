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
        return sqlite3.sqlite_version

    async def drop(self) -> None:
        try:
            self.remove_files()
        except OSError as error:
            print(f"(the file {self.path} stays: {error})")
