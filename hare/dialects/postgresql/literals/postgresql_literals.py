from __future__ import annotations

import datetime
from collections.abc import Callable, Collection, Sequence
from typing import TYPE_CHECKING, Any

from hare.ddl.enums import SchemaNamespace, SchemaPartType
from hare.dialects.base.literals.sql_literals import SqlLiterals
from hare.fields.data.containers.array_field import ArrayField
from hare.time.system_zone import SystemZone

if TYPE_CHECKING:
    from hare.fields.field import Field


class PostgresqlLiterals(SqlLiterals):
    """PostgreSQL's names and literals - double-quoted aliases, escape strings, ``bytea`` and array
    literals."""

    alias_quote_char = '"'

    def get_schema_object_key(
        self, object_type: SchemaPartType, schema: str | None, table: str, name: str
    ) -> tuple[SchemaNamespace, str | None, str | None, str] | None:
        """Compare only objects sharing a physical name scope on this database."""
        if object_type is SchemaPartType.TRIGGER:
            return self._get_schema_object_key(SchemaNamespace.TRIGGER, schema, table, name)
        if object_type in (
            SchemaPartType.TABLE,
            SchemaPartType.INDEX,
            SchemaPartType.VIEW,
            SchemaPartType.MATERIALIZED_VIEW,
            SchemaPartType.SEQUENCE,
        ):
            return self._get_schema_object_key(SchemaNamespace.RELATION, schema, None, name)
        return None

    def get_field_literal_sql(self, field: Field[Any], value: Any) -> str:
        """Use a typed ARRAY constructor without assuming the element type's text delimiter."""
        value_field = self.dialect.types.get_value_field(field)
        if isinstance(value_field, ArrayField) and value is not None:
            return f"({self.get_array_constructor_sql(value)})::{value_field.get_column_type(self.dialect)}"
        return super().get_field_literal_sql(field, value)

    def get_array_constructor_sql(self, value: Collection[Any]) -> str:
        """Render converted array elements recursively using their ordinary scalar literals."""
        return self.get_array_literal_sql(
            [
                self.get_array_constructor_sql(element)
                if isinstance(element, (list, tuple, set, frozenset))
                else self.get_literal_sql(element)
                for element in value
            ]
        )

    def get_string_literal_sql(self, text: str) -> str:
        """A string literal PostgreSQL reads the same whatever ``standard_conforming_strings`` is
        set to: ``'...'`` without a backslash, an escape string ``E'...'`` with one."""
        literal = super().get_string_literal_sql(text)
        if "\\" not in literal:
            return literal
        return "E" + literal.replace("\\", "\\\\")

    def get_literal_writers(self) -> dict[type, Callable[[Any], str]]:
        """A boolean is ``TRUE``/``FALSE``; a naive datetime is read in local system time and a
        naive time in UTC, as both drivers bind them; a list, tuple or set is an array literal."""
        return {
            **super().get_literal_writers(),
            datetime.datetime: self.get_local_moment_literal_sql,
            datetime.time: self.get_utc_time_literal_sql,
            **dict.fromkeys((list, tuple, set, frozenset), self.get_array_text_literal_sql),
        }

    def get_stored_boolean_literal_sql(self, value: bool) -> str:
        return "TRUE" if value else "FALSE"

    def get_local_moment_literal_sql(self, value: datetime.datetime) -> str:
        """A moment as its ISO text - a naive one read in local system time.

        Args:
            value: The moment.

        Returns:
            The literal.
        """
        if value.tzinfo is None:
            value = SystemZone.make_system_local_aware(value)
        return self.get_iso_literal_sql(value)

    def get_utc_time_literal_sql(self, value: datetime.time) -> str:
        """A time as its ISO text - a naive one read in UTC.

        Args:
            value: The time.

        Returns:
            The literal.
        """
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.UTC)
        return self.get_iso_literal_sql(value)

    def get_array_text_literal_sql(self, value: Collection[Any]) -> str:
        """A list, a tuple or a set as the text PostgreSQL reads an array from.

        Args:
            value: The elements.

        Returns:
            The literal.
        """
        return "'{" + ",".join(self.get_array_element_literal_sql(item) for item in value) + "}'"

    def get_array_element_literal_sql(self, value: Any) -> str:
        """One element of an array literal, which is itself inside a single-quoted literal.

        Args:
            value: The element.

        Returns:
            The element's text.
        """
        if isinstance(value, str):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("'", "''")
            return f'"{escaped}"'
        if isinstance(value, (list, tuple, set, frozenset)):
            return "{" + ",".join(self.get_array_element_literal_sql(item) for item in value) + "}"
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return "NULL"
        return str(value)

    def get_bytes_literal_sql(self, value: bytes) -> str:
        return f"'\\x{value.hex()}'::bytea"

    def get_array_literal_sql(self, element_sqls: Sequence[str]) -> str:
        return f"ARRAY[{','.join(element_sqls)}]" if element_sqls else "'{}'"
