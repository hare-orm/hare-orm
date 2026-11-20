from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, ClassVar
from zoneinfo import ZoneInfo

from hare.dialects.sqlite.constants import SQLITE_EXTRACT_FUNCTION_NAME, SQLITE_TRUNC_FUNCTION_NAME
from hare.dialects.sqlite.functions.datetime.date_truncation import DateTruncation
from hare.dialects.sqlite.functions.datetime.sqlite_date_part_extraction import SqliteDatePartExtraction
from hare.time.zone_names import ZoneNames
from rust.native import sqlite_functions

if TYPE_CHECKING:
    import aiosqlite

    from hare.dialects.sqlite.functions.sqlite_function_errors import SqliteFunctionErrors


class SqliteNativeFunctions:
    """The compiled ``rust.native.sqlite_functions`` module - the SQLite functions registered on a
    connection. Each runs the common inputs natively and hands the rest to its Python function."""

    #: The module.
    module = sqlite_functions
    #: Holds the zone cache bucket shared by extraction and truncation.
    cache_buckets: ClassVar[dict[int, Any]] = {}
    #: Date/timestamp comparison and local Now both accept case-insensitive IANA names.
    named_zones = ZoneNames.ZONES.new_shared_bucket()

    @staticmethod
    async def install_fallback_group(
        connection: aiosqlite.Connection,
        function_errors: SqliteFunctionErrors,
        functions: Mapping[str, tuple[int, Callable[..., Any]]],
        constructor: Callable[[dict[str, tuple[int, Callable[..., Any]]]], Any],
        prefix: str,
    ) -> None:
        """Register deterministic native functions with the connection's Python error channel."""
        wrapped = {name: (arity, function_errors.wrap(callback)) for name, (arity, callback) in functions.items()}
        native = constructor(wrapped)
        for name, (arity, _) in wrapped.items():
            await connection.create_function(f"{prefix}{name}", arity, getattr(native, name), deterministic=True)

    @classmethod
    async def install_dates(cls, connection: aiosqlite.Connection, function_errors: SqliteFunctionErrors) -> None:
        """Register both date functions using the connection's error channel and one zone cache."""
        dates = cls.module.DateFunctions(
            ZoneInfo,
            ZoneNames.ZONES.get_bucket(cls),
            function_errors.wrap(SqliteDatePartExtraction.extract),
            function_errors.wrap(DateTruncation.truncate),
        )
        await connection.create_function(SQLITE_EXTRACT_FUNCTION_NAME, 3, dates.extract)
        await connection.create_function(SQLITE_TRUNC_FUNCTION_NAME, 3, dates.truncate, deterministic=True)
