from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, ClassVar, cast

from hare.dialects.base.clauses.limit_returning_conflict_query_clauses import LimitReturningConflictQueryClauses
from hare.dialects.sqlite.clauses.constants import (
    SQLITE_GLOB_SPECIAL_CHARACTERS,
    SQLITE_GLOB_WILDCARDS_BY_LIKE_WILDCARD,
    SQLITE_LIKE_ESCAPE_CHARACTER,
)
from hare.exceptions import UnSupportedError
from hare.sql.constants import DEFAULT_LIKE_ESCAPE_CLAUSE
from hare.sql.functions.text.upper import Upper
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.features import Features
    from hare.sql.builder.returned_value import ReturnedValue
    from hare.sql.sql_context import SqlContext
    from hare.sql.terms.criteria.like import Like
    from hare.sql.terms.term import Term


class SqliteQueryClauses(LimitReturningConflictQueryClauses):
    """SQLite's clauses - ``LIMIT -1`` for an ``OFFSET`` alone, a returned column always under its
    name, ``EXPLAIN QUERY PLAN``."""

    # SQLite accepts no OFFSET without a LIMIT; a negative one means none.
    unbounded_limit_sql: ClassVar[str | None] = "-1"

    def get_returned_value_sql(self, value: ReturnedValue) -> str:
        # SQLite before 3.36 names a returned column of a quoted name with its quotes ("id"), so a
        # plain column is returned under its own name, which every version reads back the same.
        if value.alias_sql is None and value.column_name is not None:
            return f"{value.sql} AS {self.dialect.literals.quote_identifier(value.column_name)}"
        return super().get_returned_value_sql(value)

    def get_condition_like_sql(self, like: Like, sql_context: SqlContext) -> str | None:
        """A ``LIKE`` of a condition as ``GLOB`` - SQLite's ``LIKE`` under ``case_sensitive_like`` isn't
        deterministic, which a condition of an index or a constraint must be; ``GLOB`` matches as
        that ``LIKE`` does, case-sensitively.

        Args:
            like: The ``LIKE``.
            sql_context: The context it renders in.

        Returns:
            The ``GLOB``.

        Raises:
            UnSupportedError: The pattern is read from a column, or escapes with another character.
        """
        pattern = like.right
        upper = isinstance(pattern, Upper)
        if upper:
            pattern = cast("Upper", pattern).args[0]
        if (
            not isinstance(pattern, ValueWrapper)
            or not isinstance(pattern.value, str)
            or str(like.escape) != DEFAULT_LIKE_ESCAPE_CLAUSE
        ):
            raise UnSupportedError(
                "A condition of an index or a constraint on SQLite matches a text by a literal pattern only - "
                "its LIKE isn't deterministic there, and is written as GLOB"
            )
        glob_pattern: Term = ValueWrapper(SqliteQueryClauses.get_glob_pattern(pattern.value))
        if upper:
            glob_pattern = Upper(glob_pattern)
        return f"{like.left.get_sql(sql_context)} GLOB {glob_pattern.get_sql(sql_context)}"

    @staticmethod
    def get_glob_pattern(like_pattern: str) -> str:
        """A ``LIKE`` pattern escaped by a backslash as the ``GLOB`` pattern matching the same texts.

        Args:
            like_pattern: The ``LIKE`` pattern.

        Returns:
            The ``GLOB`` pattern - ``*`` for ``%``, ``?`` for ``_``, a ``GLOB`` wildcard in the text
            matched by a bracket of its own.
        """
        glob_parts: list[str] = []
        characters = iter(like_pattern)
        for character in characters:
            if character == SQLITE_LIKE_ESCAPE_CHARACTER:
                character = next(characters, "")
            elif character in SQLITE_GLOB_WILDCARDS_BY_LIKE_WILDCARD:
                glob_parts.append(SQLITE_GLOB_WILDCARDS_BY_LIKE_WILDCARD[character])
                continue
            glob_parts.append(f"[{character}]" if character in SQLITE_GLOB_SPECIAL_CHARACTERS else character)
        return "".join(glob_parts)

    def get_explain_sql(
        self, sql: str, output_format: str | None, options: Mapping[str, bool], features: Features
    ) -> str:
        if output_format:
            raise UnSupportedError("SQLite does not support different explain formats")
        if options:
            raise UnSupportedError("SQLite does not support explain options")
        return f"EXPLAIN QUERY PLAN {sql}"
