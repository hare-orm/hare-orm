from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.dictionary import Dictionary
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_DICTIONARY_DEFAULT_LAYOUT,
    CLICKHOUSE_DICTIONARY_LAYOUT_PATTERN,
    CLICKHOUSE_DICTIONARY_LIFETIME_LIMIT,
)
from hare.exceptions import ConfigurationError


@dataclass(frozen=True)
class ClickhouseDictionary(Dictionary):
    """A ClickHouse dictionary - rows of the model's table the server keeps in memory, a value of
    them read by its key with ``DictGet`` in place of a JOIN::

        class Country(Model):
            code = fields.CharField(max_length=2, primary_key=True)
            name = fields.CharField(max_length=50)

            class Meta:
                dictionaries = [ClickhouseDictionary("country_names", key=("code",), attributes=("name",))]

    The dictionary is loaded from the model's table by the connection's own user, and again after
    ``lifetime`` - a row written to the table is read through the dictionary after its next load
    (``reload_dictionary()`` loads it at once).

    Args:
        name: The dictionary's name.
        key: The fields of the model a row is looked up by.
        attributes: The fields of the model a lookup gives.
        layout: How the server keeps the rows, with its arguments - ``"COMPLEX_KEY_HASHED()"`` takes
            a key of any fields; ``"HASHED()"``, ``"FLAT()"`` a key of one unsigned 64-bit integer.
        lifetime: After how many seconds the dictionary is loaded again - a number, or the least
            and the greatest of them (the server picks a moment in between); 0 for never.
        source: ``RawSQLTerm`` of another source than the model's table, as ``SOURCE(...)`` takes it
            - ``RawSQLTerm("POSTGRESQL(NAME orders_database TABLE 'country')")``. A ``{env:NAME}``
            in it is replaced by the environment variable ``NAME`` when the dictionary is created -
            a password stays out of the migration file.

    Raises:
        ConfigurationError: See ``Dictionary``; ``layout`` isn't a layout with its arguments,
            ``lifetime`` isn't a number of seconds or a pair of them within ten years, ``source``
            isn't ``RawSQLTerm`` of SQL.
    """

    layout: str = CLICKHOUSE_DICTIONARY_DEFAULT_LAYOUT
    lifetime: int | tuple[int, int] = 0
    source: RawSQLTerm | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        owner = f"ClickhouseDictionary {self.name!r}"
        if not isinstance(self.layout, str) or not CLICKHOUSE_DICTIONARY_LAYOUT_PATTERN.fullmatch(self.layout):
            raise ConfigurationError(
                f'{owner}: layout takes a layout with its arguments - "HASHED()", "CACHE(SIZE_IN_CELLS 1000)" - '
                f"got {self.layout!r}"
            )
        seconds = self.lifetime if isinstance(self.lifetime, tuple) else (self.lifetime,)
        if (
            len(seconds) not in {1, 2}
            or not all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= CLICKHOUSE_DICTIONARY_LIFETIME_LIMIT
                for value in seconds
            )
            or seconds[0] > seconds[-1]
        ):
            raise ConfigurationError(
                f"{owner}: lifetime takes a number of seconds from 0 to {CLICKHOUSE_DICTIONARY_LIFETIME_LIMIT}, or "
                f"the least and the greatest of them, got {self.lifetime!r}"
            )
        if self.source is not None and (not isinstance(self.source, RawSQLTerm) or not self.source.sql.strip()):
            raise ConfigurationError(
                f"{owner}: source takes RawSQLTerm(...) of a source as SOURCE(...) takes it, got {self.source!r}"
            )

    def get_lifetime_range(self) -> tuple[int, int]:
        """The seconds the dictionary is loaded again after.

        Returns:
            The least and the greatest of them.
        """
        if isinstance(self.lifetime, tuple):
            return self.lifetime
        return 0, self.lifetime

    def get_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if self.layout != CLICKHOUSE_DICTIONARY_DEFAULT_LAYOUT:
            options["layout"] = self.layout
        if self.lifetime != 0:
            options["lifetime"] = self.lifetime
        if self.source is not None:
            options["source"] = self.source
        return options
