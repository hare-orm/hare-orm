"""Sqlcommenter-style tags appended to the SQL as a comment - pg_stat_statements, auto_explain and
slow-query logs can tell the call site.
"""

from __future__ import annotations

import contextvars
from collections.abc import Generator
from contextlib import contextmanager
from urllib.parse import quote


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
        single `with` block can't span)."""
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
    def append(cls, sql: str) -> str:
        """Appends the current tags to ``sql`` as a trailing comment - per the spec, never prepended.
        Returns ``sql`` itself without tags.
        """
        tags = cls.current.get()
        if not tags:
            return sql
        # Keys and values are both escaped - a "*/" would close the comment and let the rest be SQL.
        comment = ",".join(
            f"{quote(str(key), safe='')}='{quote(str(value), safe='')}'" for key, value in sorted(tags.items())
        )
        return f"{sql} /*{comment}*/"
