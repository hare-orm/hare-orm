from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

from hare.classes.class_path import ClassPath
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import ConfigurationError


@dataclasses.dataclass(frozen=True)
class ClickhouseProjection:
    """A projection of a ClickHouse table - its rows kept a second time inside each part, sorted or
    aggregated as a query of the table says, which the server reads in place of the table where a query
    fits it::

        ClickhouseTableOptions(
            projections=(ClickhouseProjection("by_site", RawSQLTerm("SELECT site, count() GROUP BY site")),)
        )

    Attributes:
        name: The projection's name.
        query: ``RawSQLTerm`` of its ``SELECT`` - without ``FROM``, with ``GROUP BY`` or ``ORDER BY``.

    Raises:
        ConfigurationError: ``name`` is empty, or ``query`` isn't ``RawSQLTerm`` of a ``SELECT``.
    """

    name: str
    query: RawSQLTerm

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ConfigurationError(f"ClickhouseProjection.name must be a non-empty string, got {self.name!r}")
        if not isinstance(self.query, RawSQLTerm) or not self.query.sql.strip().upper().startswith("SELECT"):
            raise ConfigurationError(
                f"ClickhouseProjection.query takes RawSQLTerm(...) of a SELECT of the table, got {self.query!r}"
            )

    def get_definition_sql(self, quote: Callable[[str], str]) -> str:
        """The projection as ``ADD PROJECTION`` declares it.

        Args:
            quote: Quotes an identifier.

        Returns:
            ``name (SELECT ...)``.
        """
        return f"{quote(self.name)} ({self.query.sql.strip()})"

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """How a migration file rebuilds the projection.

        Returns:
            The class path, no positional arguments, and the keyword arguments.
        """
        return ClassPath.get(type(self)), [], {"name": self.name, "query": self.query}
