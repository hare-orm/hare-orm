from enum import StrEnum


class SqliteRegexMatching(StrEnum):
    POSIX_REGEX = " REGEXP "
    IPOSIX_REGEX = " MATCH "
