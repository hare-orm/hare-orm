from __future__ import annotations

from hare.classes.declared_subclass import DeclaredSubclass
from hare.dialects.sqlite.functions.statistics.sqlite_statistic import SqliteStatistic

SqliteVarPop = DeclaredSubclass.make(SqliteStatistic, "SqliteVarPop", __package__)


SqliteStdDevPop = DeclaredSubclass.make(
    SqliteStatistic,
    "SqliteStdDevPop",
    __package__,
    is_deviation=True,
)


SqliteStdDevSamp = DeclaredSubclass.make(
    SqliteStatistic,
    "SqliteStdDevSamp",
    __package__,
    is_deviation=True,
    sample=True,
)


SqliteVarSamp = DeclaredSubclass.make(
    SqliteStatistic,
    "SqliteVarSamp",
    __package__,
    sample=True,
)
