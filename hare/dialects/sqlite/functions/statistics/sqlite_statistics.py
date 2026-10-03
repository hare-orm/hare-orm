from decimal import Decimal
from functools import partial
from typing import Any, ClassVar

import aiosqlite

from hare.dialects.sqlite.constants import SQLITE_STATISTICS_FUNCTION_NAMES
from hare.dialects.sqlite.functions.native_functions import SqliteNativeFunctions
from hare.dialects.sqlite.functions.statistics.declarations import SqliteStdDevPop, SqliteStdDevSamp, SqliteVarSamp
from hare.dialects.sqlite.functions.statistics.sqlite_statistic import SqliteStatistic, SqliteVarPop


class SqliteStatistics:
    """Registers the statistic aggregates on SQLite connections."""

    #: Postgres function name to the class computing it.
    CLASSES: ClassVar[dict[str, type[SqliteStatistic]]] = {
        "STDDEV_POP": SqliteStdDevPop,
        "STDDEV_SAMP": SqliteStdDevSamp,
        "VAR_POP": SqliteVarPop,
        "VAR_SAMP": SqliteVarSamp,
    }

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers every statistic as a window function, usable as a plain aggregate too."""
        native_functions = SqliteNativeFunctions.module
        for postgres_name, statistic_class in cls.CLASSES.items():
            aggregate: Any = statistic_class
            if native_functions is not None:
                aggregate = partial(
                    native_functions.Statistic,
                    statistic_class.sample,
                    statistic_class.is_deviation,
                    SqliteStatistic.get_statistic,
                    Decimal,
                )
            # aiosqlite wraps create_function() but not create_window_function().
            await connection._execute(  # type: ignore[no-untyped-call]
                connection._conn.create_window_function,
                SQLITE_STATISTICS_FUNCTION_NAMES[postgres_name],
                1,
                aggregate,
            )
