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
    REGEX_PYTHON_ERROR_MESSAGES,
)
from hare.dialects.sqlite.functions.regex.sqlite_regex_translation import SqliteRegexTranslation
from hare.dialects.sqlite.functions.sqlite_function_errors import SqliteFunctionErrors
from hare.query.filters.lookups.lookups import Lookups
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.term import Term


class SqlitePosixRegex:
    """Matches a ``__posix_regex``/``__iposix_regex`` pattern on SQLite as a Postgres (UTF8)
    regular expression matches - the pattern translated into a Python ``re`` one by
    ``SqliteRegexTranslation``, an invalid one refused with Postgres's message."""

    #: (pattern, case-insensitive) -> the compiled pattern.
    PATTERNS: ClassVar[Cache[re.Pattern[str]]] = Cache(REGEX_PATTERN_CACHE_SIZE)

    @staticmethod
    def translate(pattern: str, case_insensitive: bool) -> str:
        """Translates a Postgres regular expression into a Python ``re`` pattern (compiled with DOTALL).

        Args:
            pattern: The pattern.
            case_insensitive: Whether the match ignores case.

        Returns:
            The ``re`` pattern.

        Raises:
            ValueError: The pattern isn't a valid Postgres regular expression.
        """
        return SqliteRegexTranslation(pattern, case_insensitive).translate()

    @classmethod
    def compile(cls, pattern: str, case_insensitive: bool) -> re.Pattern[str]:
        """Compiles a Postgres-style pattern.

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
                compiled = re.compile(cls.translate(pattern, case_insensitive), re.DOTALL)
            except re.error as error:
                message = REGEX_PYTHON_ERROR_MESSAGES.get(error.msg, error.msg)
                raise ValueError(REGEX_ERROR_PREFIX + message) from None
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
        """``field__posix_regex=value`` on SQLite - matched by Python's ``re`` with the pattern
        translated to Postgres semantics. ``re`` isn't resistant to catastrophic backtracking: a
        pattern as short as ``"(a+)+$"`` can hang the connection, so never pass unvalidated user
        input as ``value``.
        """
        return Lookups.regex_criterion(SqliteRegexMatching.POSIX_REGEX, term, value, text_function)

    @staticmethod
    def insensitive_posix_regex(
        term: Term, value: str, text_function: Callable[[Term], Term] | None = None
    ) -> BasicCriterion:
        """``field__iposix_regex=value`` on SQLite - see ``posix_regex()``'s own docstring for
        the same ReDoS caveat. A character of the pattern matches itself and its simple uppercase and
        lowercase forms, like Postgres's ``~*`` (``i`` matches ``I`` but not ``İ``)."""
        return Lookups.regex_criterion(SqliteRegexMatching.IPOSIX_REGEX, term, value, text_function)

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
