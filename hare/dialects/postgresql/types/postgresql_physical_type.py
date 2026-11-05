from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from hare.core.caching.cache import Cache
from hare.dialects.postgresql.types.constants import (
    POSTGRESQL_ARRAY_TYPE_SUFFIX,
    POSTGRESQL_CANONICAL_TYPE_NAMES,
    POSTGRESQL_DEFAULT_CHARACTER_LENGTH,
    POSTGRESQL_PHYSICAL_TYPE_CACHE_SIZE,
    POSTGRESQL_TYPE_RE,
)


@dataclass(frozen=True, slots=True)
class PostgresqlPhysicalType:
    """A declared physical type, preserving opaque names and absent modifiers.

    Parse the dialect's existing declaration grammar once for introspection, COPY
    and rewrite analysis. ``name=None`` means that the declaration is opaque;
    its exact text survives, including case-sensitive quoted names. Array nesting
    is explicit, but PostgreSQL type identity does not encode the declared rank.
    Wire OIDs and domain/range structure remain in tokio-postgres's Type tree:
    that metadata has no column typmod and must not supply precision or length.
    """

    sql: str
    name: str | None = None
    parameters: tuple[str, ...] | None = None
    array_dimensions: int = 0
    element: PostgresqlPhysicalType | None = None

    parsed: ClassVar[Cache[PostgresqlPhysicalType]] = Cache(POSTGRESQL_PHYSICAL_TYPE_CACHE_SIZE, holds_sql=False)

    @classmethod
    def from_sql(cls, sql: str) -> PostgresqlPhysicalType:
        """Read a full declaration without interpreting unsupported type modifiers."""
        sql = sql.strip()
        cached = cls.parsed.get((sql,))
        if cached is not None:
            return cached
        array = POSTGRESQL_ARRAY_TYPE_SUFFIX.search(sql)
        match = POSTGRESQL_TYPE_RE.fullmatch(sql) if array is None else None
        if array is not None:
            element = cls.from_sql(sql[: array.start()])
            result = cls(sql, element.name, element.parameters, array.group().count("["), element)
        elif match is None:
            result = cls(sql)
        else:
            name = " ".join(match.group("name").lower().split())
            if match.group("time_zone"):
                name += " " + " ".join(match.group("time_zone").lower().split())
            arguments = match.group("parameters")
            parameters = None if arguments is None else tuple(value.strip() for value in arguments.split(","))
            result = cls(sql, name, parameters)
        cls.parsed[(sql,)] = result
        return result

    @property
    def canonical_name(self) -> str | None:
        """A known built-in's catalog spelling; no identity is inferred for custom types."""
        return POSTGRESQL_CANONICAL_TYPE_NAMES.get(self.name) if self.name is not None else None

    @property
    def canonical_sql(self) -> str | None:
        """Render known declarations as format_type does, collapsing declared array ranks."""
        if self.element is not None:
            scalar = self.element.canonical_sql
            return scalar + "[]" if scalar is not None else None
        name = self.canonical_name
        if name is None:
            return None
        parameters = self.parameters
        if name == "character" and parameters is None:
            parameters = (POSTGRESQL_DEFAULT_CHARACTER_LENGTH,)
        if parameters:
            modifiers = ",".join(parameters)
            if name.startswith(("time ", "timestamp ")):
                base, _, suffix = name.partition(" ")
                return f"{base}({modifiers}) {suffix}"
            return f"{name}({modifiers})"
        return name

    @property
    def without_modifiers(self) -> str:
        """A parsed type's parameter-free spelling; opaque declarations stay untouched."""
        return self.sql if self.name is None else self.name + "[]" * self.array_dimensions

    @property
    def numeric_precision_scale(self) -> tuple[int, int] | None:
        """Explicit numeric precision/scale; NUMERIC(p) declares scale zero."""
        if self.canonical_name != "numeric" or not self.parameters or len(self.parameters) > 2:
            return None
        return int(self.parameters[0]), int(self.parameters[1]) if len(self.parameters) == 2 else 0

    @property
    def character_length(self) -> int | None:
        """The explicit length, distinct from an absent catalog or wire modifier."""
        if self.canonical_name not in {"character", "character varying"} or not self.parameters:
            return None
        return int(self.parameters[0])

    @property
    def vector_dimensions(self) -> int | None:
        """The explicit dimension of a vector column, without a default size."""
        if self.name != "vector" or self.array_dimensions or not self.parameters:
            return None
        return int(self.parameters[0])
