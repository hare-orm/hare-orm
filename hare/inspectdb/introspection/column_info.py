from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ColumnInfo:
    name: str
    db_type: str
    nullable: bool
    is_pk: bool
    is_unique: bool
    #: The column's 1-based position in the PRIMARY KEY, None outside it - the key's order can
    #: differ from the table's.
    pk_position: int | None = None
    #: A plain (non-unique) single-column index exists on this column - reconstructed as
    #: db_index=True. Independent of is_unique (a UNIQUE constraint already implies indexing at
    #: the DB level, so this only matters for a genuinely separate, non-unique index).
    has_index: bool = False
    #: character_maximum_length (Postgres) / parsed out of the declared type string (SQLite,
    #: e.g. "VARCHAR(255)") - overrides the generic max_length=255 fallback when known.
    max_length: int | None = None
    #: Precision and scale of a column mapped to a DecimalField - Postgres reports them for integers
    #: too.
    numeric_precision: int | None = None
    numeric_scale: int | None = None
    #: The parsed default (SchemaIntrospector.parse_db_default()). None: no default, a sequence
    #: default or a NULL default.
    db_default: Any = None
    #: The column's comment, where the database keeps column comments.
    description: str | None = None
    #: information_schema.columns.udt_name (Postgres): the element type of an ARRAY ("_int4"), the
    #: real type of a USER-DEFINED column.
    udt_name: str | None = None
    #: A generated column's expression, as the database's catalog gives it (SQLite: parsed from the
    #: CREATE TABLE text). A generated column whose expression can't be parsed is left out of the columns.
    generated_expression: str | None = None
    #: True for a STORED generated column, False for VIRTUAL - only meaningful when
    #: generated_expression is set.
    generated_stored: bool = True
    #: A ``vector(N)`` column's dimensions, parsed from format_type() (Postgres).
    vector_dimensions: int | None = None
    #: A ``numeric(p,s)[]`` column's element precision and scale, parsed from format_type()
    #: (Postgres).
    array_element_numeric_precision_scale: tuple[int, int] | None = None
    #: varchar[]/char[]'s own element length (Postgres only) - same reasoning as
    #: array_element_numeric_precision_scale above, parsed out of format_type()'s own
    #: "character varying(N)[]"/"character(N)[]" text.
    array_element_max_length: int | None = None
    #: "ALWAYS" or "BY DEFAULT" for a Postgres identity column (``GENERATED ... AS IDENTITY``),
    #: None otherwise - the database assigns its value on INSERT.
    identity_generation: str | None = None
    #: The column's full type as the database prints it - ``character varying(50)``, ``numeric(10,2)``,
    #: ``integer[]``; None where the introspector reads no such text.
    full_type: str | None = None
    #: The labels of the column's ``ENUM`` type in their order - of its elements' type for an array;
    #: None for a column of no enum type (Postgres only).
    enum_labels: tuple[str, ...] | None = None
    #: The schema of the column's ``ENUM`` type when it isn't the connection's default one; None otherwise
    #: (Postgres only).
    enum_type_schema: str | None = None
    #: Whether the database gives the column a value an INSERT leaves out - a sequence, an identity, a
    #: row id. Read for an integer primary key; True where it can't be told.
    generates_key: bool = True
    #: Why the column's field can't be the column as it is - written as a TODO; None when it can.
    todo_reason: str | None = None
