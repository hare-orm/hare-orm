from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.constraints.check_constraint import CheckConstraint
    from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
    from hare.ddl.schema_objects.trigger import Trigger
    from hare.ddl.table_options import TableOptions
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.composite_foreign_key_info import CompositeForeignKeyInfo
from hare.inspectdb.introspection.foreign_key_info import ForeignKeyInfo
from hare.inspectdb.introspection.index_info import IndexInfo


@dataclass
class TableInfo:
    name: str
    #: The schema this table lives in - None for a dialect without schemas (SQLite).
    schema: str | None = None
    #: Whether the table is in the connection's default schema - otherwise it is rebuilt with
    #: Meta.schema.
    is_in_default_schema: bool = True
    columns: list[ColumnInfo] = dataclass_field(default_factory=list)
    foreign_keys: dict[str, ForeignKeyInfo] = dataclass_field(default_factory=dict)
    indexes: list[IndexInfo] = dataclass_field(default_factory=list)
    #: The plain single-column indexes folded into ColumnInfo.is_unique/has_index, kept with their
    #: names so a declared constraint or index can be matched to its own database object.
    column_indexes: list[IndexInfo] = dataclass_field(default_factory=list)
    #: DB table comment (Postgres only - SQLite has no table-comment feature at all), reconstructed
    #: as Meta.table_description.
    table_description: str | None = None
    #: Function+trigger pairs (Postgres) / triggers (SQLite), successfully parsed back into a
    #: reconstructable Trigger(...) - see Meta.triggers.
    triggers: list[Trigger] = dataclass_field(default_factory=list)
    #: (name, definition) of each trigger that couldn't be parsed - written as a comment.
    unparsed_triggers: list[tuple[str, str]] = dataclass_field(default_factory=list)
    #: EXCLUDE USING ... constraints (Postgres-only, pg_constraint.contype = 'x'), successfully
    #: parsed back into a reconstructable ExclusionConstraint(...) - see Meta.constraints.
    exclusion_constraints: list[ExclusionConstraint] = dataclass_field(default_factory=list)
    #: (name, raw definition text) for an EXCLUDE constraint whose pg_get_constraintdef() text
    #: didn't fit the shape _parse_postgres_exclusion_constraint_def() can parse - same
    #: surfaced-as-a-comment pattern as unparsed_triggers.
    unparsed_exclusion_constraints: list[tuple[str, str]] = dataclass_field(default_factory=list)
    #: Composite (2+ column) foreign keys successfully reconstructed as a single
    #: ForeignKeyField(...) - see CompositeForeignKeyInfo and
    #: SchemaIntrospector.match_composite_foreign_key_naming().
    composite_foreign_keys: list[CompositeForeignKeyInfo] = dataclass_field(default_factory=list)
    #: (constraint name, columns) of each composite foreign key that couldn't be rebuilt as a
    #: ForeignKeyField - its columns become plain fields, the constraint a comment.
    unparsed_foreign_keys: list[tuple[str, tuple[str, ...]]] = dataclass_field(default_factory=list)

    @property
    def is_many_to_many_junction_shape(self) -> bool:
        """Whether the table has hare's many-to-many through table shape - every column in a foreign
        key, two or more of them. Such a table is rebuilt as a many-to-many field, not as a model.
        """
        all_column_names = {column.name for column in self.columns}
        foreign_key_column_names = set(self.foreign_keys.keys()) | {
            column_name
            for composite_foreign_key in self.composite_foreign_keys
            for column_name in composite_foreign_key.columns
        }
        return len(self.columns) >= 2 and all_column_names <= foreign_key_column_names

    #: (index name, CREATE INDEX text) of each Postgres index with an expression term that couldn't
    #: be rebuilt - written as a comment.
    unparsed_indexes: list[tuple[str, str]] = dataclass_field(default_factory=list)
    #: (column name, CREATE TABLE text) of each SQLite generated column whose expression couldn't be
    #: parsed - written as a comment; the column is left out.
    unparsed_generated_columns: list[tuple[str, str]] = dataclass_field(default_factory=list)
    #: CHECK constraints (Postgres: pg_constraint.contype = 'c'; SQLite: regex-parsed out of
    #: sqlite_master.sql, since SQLite exposes no PRAGMA for constraint text), successfully
    #: parsed back into a reconstructable CheckConstraint(...) - see Meta.constraints.
    check_constraints: list[CheckConstraint] = dataclass_field(default_factory=list)
    #: (constraint name, raw definition text) for a CHECK constraint whose definition didn't fit
    #: the shape this file's own regex parsing can reconstruct - same surfaced-as-a-comment
    #: pattern as unparsed_exclusion_constraints.
    unparsed_check_constraints: list[tuple[str, str]] = dataclass_field(default_factory=list)
    #: The CHECK constraints added NOT VALID and not validated since - the existing rows may break
    #: them (Postgres only).
    not_valid_constraint_names: list[str] = dataclass_field(default_factory=list)
    #: The table's storage options as its dialect's ``TableOptions`` (``Dialect.table_options_class``),
    #: None when every option has its default or the dialect has none - reconstructed as
    #: ``Meta.table_options``.
    table_options: TableOptions | None = None
    #: The tablespace a table without one of its own is stored in - the database's default
    #: (Postgres only).
    default_tablespace: str | None = None
