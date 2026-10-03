from hare.dialects.sqlite.functions.statistics.sqlite_statistic import SqliteStatistic
from hare.utils.declared_subclass import DeclaredSubclass

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
