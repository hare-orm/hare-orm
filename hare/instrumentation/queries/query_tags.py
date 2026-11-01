"""Sqlcommenter-style tags appended to the SQL as a comment - a database's statement statistics, plan
logs and slow-query logs can tell the call site.
"""

from __future__ import annotations

import contextvars
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING
from urllib.parse import quote

from hare.exceptions import ConfigurationError
from hare.instrumentation.constants import (
    BLOCK_COMMENT_END,
    BLOCK_COMMENT_START,
    INSERT_KEYWORD,
    INSERT_KEYWORD_LENGTH,
    LINE_COMMENT_START,
)

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect


class QueryTags:
    """Tags the SQL statements executed inside a ``QueryTags.scope()`` block with a trailing
    sqlcommenter-style comment."""

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
        """Tags every statement of the block with the given pairs, as a trailing comment (``with
        QueryTags.scope(application="billing"):``). A nested block has only its own tags.
        """
        token = cls.set(tags)
        try:
            yield
        finally:
            cls.reset(token)

    @classmethod
    def append(cls, sql: str, dialect: Dialect | None = None) -> str:
        """Appends the current tags to ``sql`` as a trailing comment - per the spec, never prepended,
        but on an INSERT of a database reading what follows its VALUES as data
        (``Features.reads_trailing_comment_of_insert`` off), which takes it first. Returns ``sql``
        itself without tags.

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
        if (
            dialect is not None
            and not dialect.features.reads_trailing_comment_of_insert
            and cls.starts_with_insert(sql)
        ):
            return f"/*{comment}*/ {sql}"
        # On a line of its own after a line comment, which would take it in.
        separator = "\n" if LINE_COMMENT_START in sql[sql.rfind("\n") + 1 :] else " "
        return f"{sql}{separator}/*{comment}*/"

    @staticmethod
    def starts_with_insert(sql: str) -> bool:
        """Whether a statement is an INSERT - the comments it starts with skipped.

        Args:
            sql: The statement.

        Returns:
            True for an INSERT.
        """
        position = 0
        while True:
            while position < len(sql) and sql[position].isspace():
                position += 1
            if sql.startswith(LINE_COMMENT_START, position):
                line_end = sql.find("\n", position)
                if line_end == -1:
                    return False
                position = line_end + 1
            elif sql.startswith(BLOCK_COMMENT_START, position):
                comment_end = sql.find(BLOCK_COMMENT_END, position + len(BLOCK_COMMENT_START))
                if comment_end == -1:
                    return False
                position = comment_end + len(BLOCK_COMMENT_END)
            else:
                return sql[position : position + INSERT_KEYWORD_LENGTH].upper() == INSERT_KEYWORD
