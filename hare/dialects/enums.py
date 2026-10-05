from __future__ import annotations

from enum import StrEnum


class DialectName(StrEnum):
    """The SQL dialect a client, schema editor or migration targets."""

    SQL = "sql"
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


class ParameterPosition(StrEnum):
    """Where a bound literal stands in a statement - a dialect decides from it whether the database
    can type the parameter on its own, and what to type it as otherwise."""

    CASE_BRANCH = "case_branch"
    RAW_SQL = "raw_sql"
    SELECTED_VALUE = "selected_value"
    COMPARED_VALUE = "compared_value"
    FUNCTION_ARGUMENT = "function_argument"
    TEXT_FUNCTION_ARGUMENT = "text_function_argument"


class ConnectionOptionType(StrEnum):
    """The type of value a connection setting takes."""

    WHOLE_NUMBER = "whole_number"
    SECONDS = "seconds"
    BOOLEAN = "boolean"
    CHOICE = "choice"
    TEXT = "text"
    CALLABLE = "callable"
