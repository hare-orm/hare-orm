from enum import StrEnum


class PostgresqlArrayOperators(StrEnum):
    CONTAINS = "@>"
    CONTAINED_BY = "<@"
    OVERLAP = "&&"


class HStoreOperators(StrEnum):
    CONTAINS = "@>"
    CONTAINED_BY = "<@"
    HAS_KEY = "?"
    HAS_KEYS = "?&"
    HAS_ANY_KEYS = "?|"


class PostgresqlRangeOperators(StrEnum):
    CONTAINS = "@>"
    CONTAINED_BY = "<@"
    OVERLAP = "&&"
    FULLY_LT = "<<"
    FULLY_GT = ">>"
    NOT_LT = "&>"
    NOT_GT = "&<"
    ADJACENT_TO = "-|-"


class PostgresqlLookup(StrEnum):
    """Lookup suffixes only PostgreSQL has - a range's (``days__fully_lt``) and the trigram ones
    (``name__trigram_similar``)."""

    FULLY_LT = "fully_lt"
    FULLY_GT = "fully_gt"
    NOT_LT = "not_lt"
    NOT_GT = "not_gt"
    ADJACENT_TO = "adjacent_to"
    TRIGRAM_SIMILAR = "trigram_similar"
    TRIGRAM_WORD_SIMILAR = "trigram_word_similar"
    TRIGRAM_STRICT_WORD_SIMILAR = "trigram_strict_word_similar"


class PostgresqlRegexMatching(StrEnum):
    POSIX_REGEX = " ~ "
    IPOSIX_REGEX = " ~* "


class PostgresqlTrigramMatching(StrEnum):
    SIMILAR = " % "
    WORD_SIMILAR = " %> "
    STRICT_WORD_SIMILAR = " %>> "


class PartitionStrategy(StrEnum):
    """How PostgreSQL splits a partitioned table's rows among its partitions."""

    HASH = "HASH"
    LIST = "LIST"
    RANGE = "RANGE"


class RangeBound(StrEnum):
    """A bound of a ``RangePartition`` below or above every value of its column."""

    MINVALUE = "MINVALUE"
    MAXVALUE = "MAXVALUE"
