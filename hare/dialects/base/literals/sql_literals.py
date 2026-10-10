from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.cache import Cache
from hare.dialects.base.literals.constants import SQL_NULL_BYTE, SQL_NULL_BYTE_MESSAGE
from hare.exceptions import ValidationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect


class SqlLiterals:
    """How a dialect writes names and values into the SQL text: quoted identifiers and literals.

    Attributes:
        identifier_quote_char: The character a table, column, index or constraint name is quoted
            with, a doubled one inside the name standing for itself.
        alias_quote_char: The character a SELECT alias is quoted with, empty for a bare alias.
    """

    identifier_quote_char: str = '"'
    alias_quote_char: str = ""

    #: The writer each set of literals found for a value's class through the class's bases, kept
    #: in a bucket of the set.
    found_literal_writers: ClassVar[Cache[Any]] = Cache(holds_sql=False, keyed_by_model=False)

    def __init__(self, dialect: Dialect) -> None:
        """
        Args:
            dialect: The dialect whose SQL the literals are written into.
        """
        self.dialect = dialect
        #: The buckets of the caches these literals read (``Cache.get_owner_bucket()``).
        self.cache_buckets: dict[int, Any] = {}
        #: What writes a value of each class - and of its subclasses - as a literal.
        self.literal_writers: dict[type, Callable[[Any], str]] = self.get_literal_writers()
        self.literal_writers_by_value_class: dict[type, Callable[[Any], str]] = (
            SqlLiterals.found_literal_writers.get_owner_bucket(self)
        )

    def quote_identifier(self, name: str) -> str:
        """Quotes a table, column, index or constraint name for the dialect's SQL.

        Args:
            name: The name.

        Returns:
            The quoted name.
        """
        escaped = name.replace(self.identifier_quote_char, self.identifier_quote_char * 2)
        return f"{self.identifier_quote_char}{escaped}{self.identifier_quote_char}"

    def qualify_table_name(self, table_name: str, schema: str | None = None) -> str:
        """Quotes a table name, with its schema where the dialect has schemas.

        Args:
            table_name: The table.
            schema: Its schema; None for the connection's current one.

        Returns:
            The quoted name.
        """
        if schema and self.dialect.features.supports_schemas:
            return f"{self.quote_identifier(schema)}.{self.quote_identifier(table_name)}"
        return self.quote_identifier(table_name)

    def get_string_literal_sql(self, text: str) -> str:
        """A string written into the SQL text as a literal.

        Args:
            text: The string.

        Returns:
            The literal, its quotes doubled.

        Raises:
            ValidationError: The string holds a null byte.
        """
        if SQL_NULL_BYTE in text:
            raise ValidationError(SQL_NULL_BYTE_MESSAGE.format(text=text))
        return "'" + text.replace("'", "''") + "'"

    def get_boolean_literal_sql(self, value: bool) -> str:
        """A boolean written into a query's SQL text.

        Args:
            value: The boolean.

        Returns:
            ``true``/``false`` - ISO SQL's boolean literals.
        """
        return "true" if value else "false"

    def get_literal_writers(self) -> dict[type, Callable[[Any], str]]:
        """What writes a value of each class as a literal. A value of another class is written as
        the nearest of its base classes here is, ``get_unknown_literal_sql()`` when none is.

        Returns:
            The writers by class.
        """
        return {
            bool: self.get_stored_boolean_literal_sql,
            int: str,
            Decimal: str,
            float: self.get_float_literal_sql,
            str: self.get_string_literal_sql,
            datetime.datetime: self.get_iso_literal_sql,
            datetime.time: self.get_iso_literal_sql,
            datetime.date: self.get_date_literal_sql,
            datetime.timedelta: self.get_duration_literal_sql,
            bytes: self.get_bytes_literal_sql,
        }

    def get_literal_sql(self, value: Any) -> str:
        """A value written into the SQL text as a literal - a column default.

        Args:
            value: The value, already in its database form (``Field.to_db_value()``).

        Returns:
            The literal.
        """
        if value is None:
            return "NULL"
        writer = self.literal_writers_by_value_class.get(value.__class__)
        if writer is None:
            writer = self.find_literal_writer(value.__class__)
        return writer(value)

    def find_literal_writer(self, value_class: type) -> Callable[[Any], str]:
        """What writes the values of a class - found once per class.

        Args:
            value_class: The class.

        Returns:
            The writer of the nearest base class having one, ``get_unknown_literal_sql()`` for none.
        """
        writers = self.literal_writers
        writer = next((writers[base] for base in value_class.__mro__ if base in writers), self.get_unknown_literal_sql)
        self.literal_writers_by_value_class[value_class] = writer
        return writer

    def get_unknown_literal_sql(self, value: Any) -> str:
        """A value of a class no writer is known for.

        Args:
            value: The value.

        Returns:
            ``repr(value)``.
        """
        return repr(value)

    def get_stored_boolean_literal_sql(self, value: bool) -> str:
        """A boolean as a column holds it.

        Args:
            value: The boolean.

        Returns:
            ``1``/``0``.
        """
        return "1" if value else "0"

    def get_float_literal_sql(self, value: float) -> str:
        """A float with the fifteen digits it surely holds.

        Args:
            value: The float.

        Returns:
            The literal.
        """
        return f"{value:.15g}"

    def get_iso_literal_sql(self, value: datetime.datetime | datetime.time) -> str:
        """A moment or a time as its ISO text.

        Args:
            value: The moment or the time.

        Returns:
            The literal.
        """
        return self.get_string_literal_sql(value.isoformat())

    def get_date_literal_sql(self, value: datetime.date) -> str:
        """A date as ``YYYY-MM-DD``.

        Args:
            value: The date.

        Returns:
            The literal.
        """
        return self.get_string_literal_sql(f"{value.year:04}-{value.month:02}-{value.day:02}")

    def get_duration_literal_sql(self, value: datetime.timedelta) -> str:
        """A duration as the text ``get_duration_text()`` gives.

        Args:
            value: The duration.

        Returns:
            The literal.
        """
        return self.get_string_literal_sql(self.get_duration_text(value))

    @staticmethod
    def get_duration_text(duration: datetime.timedelta) -> str:
        """A duration as ``[-]HH:MM:SS[.ffffff]``.

        Args:
            duration: The duration.

        Returns:
            The text.
        """
        total_microseconds = (duration.days * 86400 + duration.seconds) * 1_000_000 + duration.microseconds
        sign = "-" if total_microseconds < 0 else ""
        total_microseconds = abs(total_microseconds)
        microseconds = total_microseconds % 1_000_000
        total_seconds = total_microseconds // 1_000_000
        text = f"{sign}{total_seconds // 3600:02d}:{(total_seconds // 60) % 60:02d}:{total_seconds % 60:02d}"
        return f"{text}.{microseconds:06d}" if microseconds else text

    def get_bytes_literal_sql(self, value: bytes) -> str:
        """A bytes value written into the SQL text."""
        return f"X'{value.hex()}'"

    def get_array_literal_sql(self, element_sqls: Sequence[str]) -> str:
        """An array written into the SQL text from its elements' SQL."""
        return f"ARRAY[{','.join(element_sqls)}]"
