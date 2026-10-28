from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.ddl.constraints.check_constraint import CheckConstraint
    from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
    from hare.ddl.enums import RowLevelSecurity
    from hare.ddl.schema_objects.database_sequence import DatabaseSequence
    from hare.ddl.schema_objects.trigger import Trigger
    from hare.ddl.security.grant import Grant
    from hare.ddl.security.policy import Policy
    from hare.ddl.table_options import TableOptions
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.composite_foreign_key_info import CompositeForeignKeyInfo
from hare.inspectdb.introspection.foreign_key_info import ForeignKeyInfo
from hare.inspectdb.introspection.index_info import IndexInfo


@dataclass
class TableInfo:
    """What ``inspectdb`` reads of a table: its columns, keys, indexes, constraints, triggers, options, security
    and comment - what its model is rebuilt from."""

    name: str
    #: The schema this table lives in - a schema, an attached database or a database of the server: the
    #: namespace of tables the database has.
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
    #: The table's comment, where the database keeps table comments - reconstructed as
    #: Meta.table_description.
    table_description: str | None = None
    #: The table's triggers - with the functions they call where the database keeps them apart - parsed
    #: back into a reconstructable Trigger(...) - see Meta.triggers.
    triggers: list[Trigger] = dataclass_field(default_factory=list)
    #: (name, definition) of each trigger that couldn't be parsed - written as a comment.
    unparsed_triggers: list[tuple[str, str]] = dataclass_field(default_factory=list)
    #: Exclusion constraints, successfully parsed back into a reconstructable
    #: ExclusionConstraint(...) - see Meta.constraints.
    exclusion_constraints: list[ExclusionConstraint] = dataclass_field(default_factory=list)
    #: (name, raw definition text) for an exclusion constraint whose definition didn't fit the shape the
    #: dialect's parsing reconstructs - written as a comment, as unparsed_triggers.
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
        key, two or more foreign keys: one to each side. Such a table is rebuilt as a many-to-many
        field, not as a model; one composite foreign key over every column is a model of its own.
        """
        all_column_names = {column.name for column in self.columns}
        foreign_key_column_names = set(self.foreign_keys.keys()) | {
            column_name
            for composite_foreign_key in self.composite_foreign_keys
            for column_name in composite_foreign_key.columns
        }
        foreign_key_count = len(self.foreign_keys) + len(self.composite_foreign_keys)
        return foreign_key_count >= 2 and all_column_names <= foreign_key_column_names

    #: (index name, CREATE INDEX text) of each index with an expression term that couldn't
    #: be rebuilt - written as a comment.
    unparsed_indexes: list[tuple[str, str]] = dataclass_field(default_factory=list)
    #: (column name, CREATE TABLE text) of each generated column whose expression couldn't be
    #: parsed - written as a comment; the column is left out.
    unparsed_generated_columns: list[tuple[str, str]] = dataclass_field(default_factory=list)
    #: CHECK constraints - from the catalog, or parsed out of the table's CREATE TABLE text where the
    #: database keeps no other - successfully parsed back into a reconstructable CheckConstraint(...) -
    #: see Meta.constraints.
    check_constraints: list[CheckConstraint] = dataclass_field(default_factory=list)
    #: (constraint name, raw definition text) for a CHECK constraint whose definition didn't fit
    #: the shape the dialect's parsing can reconstruct - same surfaced-as-a-comment
    #: pattern as unparsed_exclusion_constraints.
    unparsed_check_constraints: list[tuple[str, str]] = dataclass_field(default_factory=list)
    #: The CHECK constraints added unchecked and not validated since - the existing rows may break
    #: them.
    not_valid_constraint_names: list[str] = dataclass_field(default_factory=list)
    #: The table's storage options as its dialect's ``TableOptions`` (``Dialect.table_options_class``),
    #: None when every option has its default or the dialect has none - reconstructed as
    #: ``Meta.table_options``.
    table_options: TableOptions | None = None
    #: How the table is partitioned when hare declares no such partitioning - a key over an expression,
    #: a partition partitioned itself - as the database writes it; None otherwise.
    unparsed_partitioning: str | None = None
    #: Whether row level security filters the table's rows - reconstructed as
    #: ``Meta.row_level_security``; None when it doesn't (and on a dialect without it).
    row_level_security: RowLevelSecurity | None = None
    #: The table's row level security policies - reconstructed as ``Meta.policies``.
    policies: list[Policy] = dataclass_field(default_factory=list)
    #: The grants on the table and its columns, the columns by their column names - reconstructed as
    #: ``Meta.grants``.
    grants: list[Grant] = dataclass_field(default_factory=list)
    #: What of the grants no ``Grant`` declares - written as comments.
    unparsed_grants: list[str] = dataclass_field(default_factory=list)
    #: The sequences a column of the table owns, ``owned_by`` the column's name - reconstructed as
    #: ``Meta.sequences``.
    sequences: list[DatabaseSequence] = dataclass_field(default_factory=list)
    #: The sequences a column default of the table reads that the model of another table declares - by
    #: name, each with that table.
    sequences_of_other_tables: list[tuple[str, str]] = dataclass_field(default_factory=list)
    #: The primary key holds its last key ``WITHOUT OVERLAPS``.
    primary_key_without_overlaps: bool = False
