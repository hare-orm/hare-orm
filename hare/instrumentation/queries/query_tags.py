"""Sqlcommenter-style tags placed in SQL comments by the dialect - a database's statement statistics, plan
logs and slow-query logs can tell the call site.
"""

from __future__ import annotations

import contextvars
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING
from urllib.parse import quote

from hare.dialects.base.clauses.query_clauses import QueryClauses
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect


class QueryTags:
    """Tags SQL statements inside a ``QueryTags.scope()`` block with a sqlcommenter-style comment.

    The dialect chooses its placement, preserving the statement syntax."""

    #: The tags active for the current task, or None if none are set - a plain dict, not this
    #: ContextVar mutated in place, so a nested scope() block can restore the OUTER block's tags
    #: on exit (mutating a shared dict in place couldn't undo a key it overwrote).
    current: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar("hare_query_tags", default=None)

    @classmethod
    def set(cls, tags: dict[str, str]) -> contextvars.Token[dict[str, str] | None]:
        """Sets the query tags active for the current task. Prefer the scope() context manager
        unless you need to manage the token's lifetime yourself (e.g. across an await boundary a
        single `with` block can't span).

        Raises:
            ConfigurationError: A key isn't a non-empty string, a value is None, or either holds a lone
                surrogate.
        """
        for key, value in tags.items():
            if not isinstance(key, str) or not key:
                raise ConfigurationError(f"A query tag is named by a non-empty string, got {key!r}")
            if value is None:
                raise ConfigurationError(f"The query tag {key!r} is given None - leave the tag out instead")
            try:
                key.encode()
                str(value).encode()
            except UnicodeEncodeError:
                # A lone surrogate - the comment is written as UTF-8 text.
                raise ConfigurationError(
                    f"The query tag {key!r} holds a character UTF-8 can't write (a lone surrogate)"
                ) from None
        return cls.current.set(tags)

    @classmethod
    def reset(cls, token: contextvars.Token[dict[str, str] | None]) -> None:
        """Undoes a set() call, restoring whatever was active before it."""
        cls.current.reset(token)

    @classmethod
    @contextmanager
    def scope(cls, **tags: str) -> Generator[None]:
        """Tags every statement of the block with the given pairs in a comment placed by its dialect
        (``with QueryTags.scope(application="billing"):``). A nested block has only its own tags.
        """
        token = cls.set(tags)
        try:
            yield
        finally:
            cls.reset(token)

    @classmethod
    def append(cls, sql: str, dialect: Dialect | None = None) -> str:
        """Format current tags and let the dialect place the comment without parsing SQL.

        Without a dialect the comment is appended. No tags returns the original string.

        Args:
            sql: The statement.
            dialect: The dialect the statement runs in - None for a comment appended in any case.

        Returns:
            The tagged statement.
        """
        tags = cls.current.get()
        if not tags:
            return sql
        # Keys and values are both escaped - a "*/" would close the comment and let the rest be SQL.
        comment = ",".join(
            f"{quote(str(key), safe='')}='{quote(str(value), safe='')}'" for key, value in sorted(tags.items())
        )
        clauses = dialect.clauses if dialect is not None else QueryClauses
        return clauses.get_tagged_statement_sql(sql, comment)
