from __future__ import annotations

from hare.dialects.postgresql.functions.aggregates import (
    BitAnd,
    BitOr,
    BitXor,
    BoolAnd,
    BoolOr,
    JSONBAgg,
    RangeAgg,
    StringAgg,
)
from hare.dialects.postgresql.functions.array import ArraySubquery
from hare.dialects.postgresql.functions.ltree import Subpath
from hare.dialects.postgresql.functions.random_uuid import RandomUUID
from hare.dialects.postgresql.functions.spatial import STDistance, STDWithin
from hare.dialects.postgresql.functions.statistics import (
    Corr,
    CovarPop,
    RegrAvgX,
    RegrAvgY,
    RegrCount,
    RegrIntercept,
    RegrR2,
    RegrSlope,
    RegrSXX,
    RegrSXY,
    RegrSYY,
)
from hare.dialects.postgresql.functions.transaction_now import TransactionNow
from hare.dialects.postgresql.functions.trigram import (
    TrigramDistance,
    TrigramSimilarity,
    TrigramStrictWordDistance,
    TrigramStrictWordSimilarity,
    TrigramWordDistance,
    TrigramWordSimilarity,
)
from hare.dialects.postgresql.postgresql_dialect import PostgresqlDialect
from hare.query.expressions.dialect_expressions import DialectExpressions

__all__ = [
    "ArraySubquery",
    "BitAnd",
    "BitOr",
    "BitXor",
    "BoolAnd",
    "BoolOr",
    "Corr",
    "CovarPop",
    "JSONBAgg",
    "RandomUUID",
    "RangeAgg",
    "RegrAvgX",
    "RegrAvgY",
    "RegrCount",
    "RegrIntercept",
    "RegrR2",
    "RegrSXX",
    "RegrSXY",
    "RegrSYY",
    "RegrSlope",
    "STDWithin",
    "STDistance",
    "StringAgg",
    "Subpath",
    "TransactionNow",
    "TrigramDistance",
    "TrigramSimilarity",
    "TrigramStrictWordDistance",
    "TrigramStrictWordSimilarity",
    "TrigramWordDistance",
    "TrigramWordSimilarity",
]

# The functions only PostgreSQL has - a query on another database refuses each before any SQL.
DialectExpressions.register(
    PostgresqlDialect,
    (
        ArraySubquery,
        BitAnd,
        BitOr,
        BitXor,
        BoolAnd,
        BoolOr,
        Corr,
        CovarPop,
        JSONBAgg,
        RandomUUID,
        RangeAgg,
        RegrAvgX,
        RegrAvgY,
        RegrCount,
        RegrIntercept,
        RegrR2,
        RegrSXX,
        RegrSXY,
        RegrSYY,
        RegrSlope,
        STDWithin,
        STDistance,
        StringAgg,
        Subpath,
        TransactionNow,
        TrigramDistance,
        TrigramSimilarity,
        TrigramStrictWordDistance,
        TrigramStrictWordSimilarity,
        TrigramWordDistance,
        TrigramWordSimilarity,
    ),
)
