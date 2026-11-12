from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from hare.core.caching.cache import Cache
from hare.dialects.clickhouse.enums import ClickhouseTypeName
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.types.constants import (
    CLICKHOUSE_DECIMAL_TYPE_PRECISIONS,
    CLICKHOUSE_PHYSICAL_TYPE_CACHE_SIZE,
    CLICKHOUSE_TYPE_CONTAINERS,
)


@dataclass(frozen=True, slots=True)
class ClickhousePhysicalType:
    """An immutable catalog or wire type, independent of a Python field's semantics.

    ``sql`` retains the original declaration, including unknown types and options.
    ``arguments`` retain scalar options verbatim; ``elements`` describe the known
    container positions. Unknown parameters are never guessed to be child types.
    Missing precision, scale and timezone stay unknown, rather than acquiring
    defaults from a field or the local machine. Catalog and wire names enter the
    same cache and use the existing dialect lexer for quoting and parentheses.
    """

    sql: str
    name: str
    arguments: tuple[str, ...] = ()
    elements: tuple[ClickhousePhysicalType, ...] = ()
    element_names: tuple[str | None, ...] = ()

    parsed: ClassVar[Cache[ClickhousePhysicalType]] = Cache(CLICKHOUSE_PHYSICAL_TYPE_CACHE_SIZE, holds_sql=False)

    @classmethod
    def from_sql(cls, sql: str) -> ClickhousePhysicalType:
        """Read a declaration once, retaining unknown declarations as opaque text."""
        sql = sql.strip()
        cached = cls.parsed.get((sql,))
        if cached is not None:
            return cached
        name = ClickhouseSqlParts.get_before_parentheses(sql)
        inside, suffix = ClickhouseSqlParts.get_parenthesised(sql)
        if suffix.strip():
            # A syntax this reader does not describe is not silently truncated.
            result = cls(sql, sql)
        else:
            arguments = tuple(ClickhouseSqlParts.split(inside))
            elements: tuple[ClickhousePhysicalType, ...] = ()
            names: tuple[str | None, ...] = ()
            if name in CLICKHOUSE_TYPE_CONTAINERS:
                if name in {ClickhouseTypeName.TUPLE, ClickhouseTypeName.NESTED}:
                    named = tuple(cls.get_element(argument) for argument in arguments)
                    names = tuple(element_name for element_name, _ in named)
                    elements = tuple(element for _, element in named)
                else:
                    elements = tuple(cls.from_sql(argument) for argument in arguments)
            result = cls(sql, name, arguments, elements, names)
        cls.parsed[(sql,)] = result
        return result

    @classmethod
    def get_element(cls, argument: str) -> tuple[str | None, ClickhousePhysicalType]:
        """Read a named or positional tuple member with the dialect's identifier lexer."""
        argument = argument.strip()
        name, rest = ClickhouseSqlParts.get_leading_identifier(argument)
        if name and rest and rest[0].isspace() and rest.strip() and not rest.lstrip().startswith("("):
            return name, cls.from_sql(rest)
        return None, cls.from_sql(argument)

    @property
    def unwrapped(self) -> ClickhousePhysicalType:
        """The value type without Nullable/LowCardinality; unknown wrapper shapes remain opaque."""
        physical_type = self
        while (
            physical_type.name in {ClickhouseTypeName.NULLABLE, ClickhouseTypeName.LOW_CARDINALITY}
            and len(physical_type.elements) == 1
        ):
            physical_type = physical_type.elements[0]
        return physical_type

    @property
    def nullable(self) -> bool:
        """Whether a Nullable wrapper is present, including inside LowCardinality."""
        return self.name == ClickhouseTypeName.NULLABLE or (
            self.name == ClickhouseTypeName.LOW_CARDINALITY and bool(self.elements) and self.elements[0].nullable
        )

    @property
    def precision(self) -> int | None:
        """Declared precision, or the fixed precision of a decimal storage width."""
        if self.name in {ClickhouseTypeName.DECIMAL, ClickhouseTypeName.DATETIME64} and self.arguments:
            return int(self.arguments[0])
        return CLICKHOUSE_DECIMAL_TYPE_PRECISIONS.get(self.name)

    @property
    def scale(self) -> int | None:
        """Declared decimal scale; unrelated or absent options remain unknown."""
        if self.name == ClickhouseTypeName.DECIMAL and len(self.arguments) == 2:
            return int(self.arguments[1])
        if self.name in CLICKHOUSE_DECIMAL_TYPE_PRECISIONS and self.arguments:
            return int(self.arguments[0])
        return None

    @property
    def timezone(self) -> str | None:
        """An explicitly declared timezone, without guessing a server/session default."""
        if self.name not in {ClickhouseTypeName.DATETIME, ClickhouseTypeName.DATETIME64}:
            return None
        position = int(self.name == ClickhouseTypeName.DATETIME64)
        return ClickhouseSqlParts.get_string_text(self.arguments[position]) if len(self.arguments) > position else None

    @staticmethod
    def get_enum_members(arguments: Sequence[str]) -> dict[str, int]:
        """Read labels and numbers using the same quoting rules as catalog declarations."""
        members = {}
        for argument in arguments:
            label, number = ClickhouseSqlParts.split(argument, separator="=")
            members[ClickhouseSqlParts.get_string_text(label)] = int(number)
        return members
