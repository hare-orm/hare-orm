from __future__ import annotations

import re
from collections.abc import Callable
from typing import ClassVar

import aiosqlite

from hare.core.caching.cache import Cache
from hare.dialects.sqlite.enums import SqliteRegexMatching
from hare.dialects.sqlite.functions.constants import (
    MAX_REGEX_PATTERN_LENGTH,
    REGEX_ERROR_PREFIX,
    REGEX_PATTERN_CACHE_SIZE,
)
from hare.dialects.sqlite.functions.regex.sqlite_regex_criterion import SqliteRegexCriterion
from hare.dialects.sqlite.functions.sqlite_function_errors import SqliteFunctionErrors
from hare.query.filters.lookups.lookups import Lookups
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.term import Term


class SqlitePosixRegex:
    """Matches a ``__posix_regex``/``__iposix_regex`` pattern on SQLite, which has no regular expressions
    of its own, with Python's ``re`` - the pattern is a Python one."""

    #: (pattern, case-insensitive) -> the compiled pattern.
    PATTERNS: ClassVar[Cache[re.Pattern[str]]] = Cache(REGEX_PATTERN_CACHE_SIZE)

    @classmethod
    def compile(cls, pattern: str, case_insensitive: bool) -> re.Pattern[str]:
        """Compiles a pattern.

        Args:
            pattern: The ``__posix_regex``/``__iposix_regex`` pattern.
            case_insensitive: Whether the match ignores case.

        Returns:
            The compiled pattern.

        Raises:
            ValueError: The pattern exceeds ``MAX_REGEX_PATTERN_LENGTH`` or isn't valid.
        """
        key = (pattern, case_insensitive)
        compiled = cls.PATTERNS.get(key)
        if compiled is None:
            if len(pattern) > MAX_REGEX_PATTERN_LENGTH:
                raise ValueError(f"regex pattern exceeds the maximum length of {MAX_REGEX_PATTERN_LENGTH} characters")
            try:
                compiled = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
            except re.error as error:
                raise ValueError(REGEX_ERROR_PREFIX + error.msg) from None
            cls.PATTERNS[key] = compiled
        return compiled

    @classmethod
    def search(cls, pattern: str | None, text: str | None, case_insensitive: bool) -> bool | None:
        """Backs the SQLite ``REGEXP``/``MATCH`` functions.

        Returns:
            Whether ``pattern`` matches somewhere in ``text``, or ``None`` when either is NULL.
        """
        # A NULL column value or pattern can't match - SQL NULL (not False) keeps both the filter
        # and its negation from matching, like every other lookup on a NULL column.
        if pattern is None or text is None:
            return None
        return cls.compile(pattern, case_insensitive).search(text) is not None

    @staticmethod
    def posix_regex(term: Term, value: str, text_function: Callable[[Term], Term] | None = None) -> BasicCriterion:
        """``field__posix_regex=value`` on SQLite - matched by Python's ``re``. ``re`` isn't resistant to
        catastrophic backtracking: a pattern as short as ``"(a+)+$"`` can hang the connection, so never
        pass unvalidated user input as ``value``.
        """
        criterion = Lookups.regex_criterion(SqliteRegexMatching.POSIX_REGEX, term, value, text_function)
        return SqliteRegexCriterion(criterion.comparator, criterion.left, criterion.right)

    @staticmethod
    def insensitive_posix_regex(
        term: Term, value: str, text_function: Callable[[Term], Term] | None = None
    ) -> BasicCriterion:
        """``field__iposix_regex=value`` on SQLite - ``re.IGNORECASE``; see ``posix_regex()``'s own
        docstring for the same ReDoS caveat."""
        criterion = Lookups.regex_criterion(SqliteRegexMatching.IPOSIX_REGEX, term, value, text_function)
        return SqliteRegexCriterion(criterion.comparator, criterion.left, criterion.right)

    @staticmethod
    async def install(connection: aiosqlite.Connection, function_errors: SqliteFunctionErrors) -> None:
        """Registers the matching functions of ``__posix_regex``/``__iposix_regex`` on ``connection`` -
        Python's ``re``. Not resistant to catastrophic backtracking; ``MAX_REGEX_PATTERN_LENGTH`` is
        only a sanity ceiling. Never pass a pattern from an untrusted source without a complexity
        limit of your own.
        """

        def regexp(pattern: str | None, text: str | None) -> bool | None:
            return SqlitePosixRegex.search(pattern, text, case_insensitive=False)

        def iregexp(pattern: str | None, text: str | None) -> bool | None:
            return SqlitePosixRegex.search(pattern, text, case_insensitive=True)

        await connection.create_function("regexp", 2, function_errors.wrap(regexp))
        await connection.create_function("match", 2, function_errors.wrap(iregexp))
