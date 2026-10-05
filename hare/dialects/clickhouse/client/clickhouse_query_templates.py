from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from hare.core.caching.cache import Cache
from hare.core.constants import CACHE_MISS
from hare.dialects.clickhouse.client.constants import CLICKHOUSE_QUERY_TEMPLATE_CACHE_MAX_SIZE
from hare.dialects.clickhouse.constants import CLICKHOUSE_PLACEHOLDER_PATTERN


class ClickhouseQueryTemplates:
    """The statements a ClickHouse client sends, each as a format string with a ``{}`` for every ``$n``
    placeholder: a statement's text is scanned for its placeholders once, not on every run."""

    #: The format string and the index of the value of each ``{}``, by the statement's text.
    templates: ClassVar[Cache[tuple[str, tuple[int, ...]]]] = Cache(
        CLICKHOUSE_QUERY_TEMPLATE_CACHE_MAX_SIZE, holds_sql=False, keyed_by_model=False
    )

    @classmethod
    def get_inlined_query(cls, query: str, values: Sequence[Any], get_literal_sql: Callable[[Any], str]) -> str:
        """The statement with each ``$n`` placeholder replaced by its value's literal - a ``$`` inside
        a quoted string or name stays.

        Args:
            query: The statement.
            values: The values, the ``n``-th for ``$n``.
            get_literal_sql: Writes a value's literal.

        Returns:
            The statement to send.
        """
        text, indexes = cls.get_cached_template(query)
        return text.format(*[get_literal_sql(values[index]) for index in indexes])

    @classmethod
    def get_cached_template(cls, query: str) -> tuple[str, tuple[int, ...]]:
        """A statement's format string, made once.

        Args:
            query: The statement.

        Returns:
            The format string, and the index of the value of each ``{}``.
        """
        key = (query,)
        template = cls.templates.get(key, CACHE_MISS)
        if template is CACHE_MISS:
            template = cls.templates[key] = cls.get_template(query)
        return template

    @staticmethod
    def get_template(query: str) -> tuple[str, tuple[int, ...]]:
        """A statement as a format string with a ``{}`` for every ``$n`` placeholder.

        Args:
            query: The statement.

        Returns:
            The format string, and the index of the value of each ``{}``.
        """
        parts: list[str] = []
        indexes: list[int] = []
        position = 0
        for match in CLICKHOUSE_PLACEHOLDER_PATTERN.finditer(query):
            number = match.group(1)
            # A quoted part stays as it is.
            if number is None:
                continue
            part = query[position : match.start()]
            # A negative literal right after a minus sign would start a "--" comment.
            if part.endswith("-"):
                part += " "
            parts.append(part.replace("{", "{{").replace("}", "}}"))
            indexes.append(int(number) - 1)
            position = match.end()
        parts.append(query[position:].replace("{", "{{").replace("}", "}}"))
        return "{}".join(parts), tuple(indexes)
