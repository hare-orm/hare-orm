from __future__ import annotations

from collections.abc import Collection, Mapping
from copy import copy
from typing import TYPE_CHECKING, Any, cast

from hare.query.hare_column_names import HareColumnNames
from hare.query.statements.constants import DISTINCT_ORDERING_COLUMN_ALIAS_PREFIX
from hare.query.statements.projections.declarations import ProjectionColumn
from hare.sql.builder.queries.set_operation_query import SetOperationQuery
from hare.sql.terms.field import Field
from hare.sql.terms.node_structure import NodeStructure
from hare.sql.terms.select_reference import SelectReference

if TYPE_CHECKING:
    from hare.fields.field import Field as ModelField
    from hare.sql.builder.queries.query_builder import QueryBuilder
    from hare.sql.terms.term import Term


class DerivedProjection:
    """Columns made available to a query outside a prepared SELECT.

    Each column retains semantic identity, value field and SQL name. The enclosing query
    chooses its output aliases through get_output().
    Hidden ordering/filter operands are added here once, without rendering SQL to
    identify them. A projection copies its builder before changing selected columns;
    the input statement and its aliases remain available to other consumers.
    """

    def __init__(
        self,
        query: QueryBuilder | SetOperationQuery,
        *,
        column_prefix: str | None = None,
        output_fields: Mapping[str, ModelField[Any] | None] | None = None,
    ) -> None:
        if isinstance(query, SetOperationQuery):
            source = copy(query)
            with_clauses, source._with = source._with, []
            names = [term.alias or cast("Field", term).name for term in source.base_query._selects]
            query = source.base_query.query_class.from_(source).select(*(source[name].as_(name) for name in names))
            query._with = with_clauses
        self.query = copy(query)
        self.column_prefix = column_prefix
        self.columns: list[ProjectionColumn] = []
        self.by_key: dict[tuple[Any, ...], ProjectionColumn] = {}
        self.by_alias: dict[str, ProjectionColumn] = {}
        self.names: set[str] = set()
        # Canonical sources of this statement, before all table references use binding identity.
        self.source_bindings = NodeStructure.get_source_bindings(query)
        selects = self.query._mutable("_selects")
        for position, term in enumerate(selects):
            alias = term.alias or (term.name if isinstance(term, Field) else None)
            name = self.get_free_name(position) if column_prefix is not None or alias is None else alias
            self.names.add(name)
            selected = term.as_(name) if column_prefix is not None or alias is None else term
            column = ProjectionColumn(
                self.get_key(term),
                name,
                selected,
                (output_fields or {}).get(alias) if alias is not None else None,
            )
            self.columns.append(column)
            self.by_key.setdefault(column.key, column)
            if alias is not None:
                self.by_alias.setdefault(alias, column)
            selects[position] = selected
        groupbys = self.query._mutable("_groupbys")
        for position, term in enumerate(groupbys):
            if isinstance(term, SelectReference) and (group_column := self.find(term.term)) is not None:
                groupbys[position] = SelectReference(group_column.term)
        self.grouped_keys = {self.get_key(term) for term in self.query._groupbys}

    def get_key(self, term: Term) -> tuple[Any, ...]:
        """The computed value, independent of a selected alias or GROUP BY position."""
        if isinstance(term, SelectReference):
            term = term.term
        return NodeStructure.get_key(term, self.source_bindings)

    def get_free_name(self, position: int) -> str:
        """Allocate a column name without collisions under case-insensitive identifiers."""
        prefix = self.column_prefix or DISTINCT_ORDERING_COLUMN_ALIAS_PREFIX
        return HareColumnNames.get_free_name(f"{prefix}{position}", (), self.names)

    def find(self, term: Term) -> ProjectionColumn | None:
        """Find a selected alias reference or a structurally equal bound value."""
        if isinstance(term, Field) and term.table is None and term.name in self.by_alias:
            return self.by_alias[term.name]
        return self.by_key.get(self.get_key(term))

    def include(self, term: Term, field: ModelField[Any] | None = None) -> ProjectionColumn:
        """Expose an operand as a hidden column and add any required grouping inputs."""
        if (column := self.find(term)) is not None:
            return column
        name = term.alias or (term.name if isinstance(term, Field) else None)
        if (
            self.column_prefix is not None
            or name is None
            or name.casefold() in {item.casefold() for item in self.names}
        ):
            name = self.get_free_name(len(self.columns))
        self.names.add(name)
        selected = term.as_(name)
        column = ProjectionColumn(self.get_key(term), name, selected, field)
        self.columns.append(column)
        self.by_key[column.key] = column
        self.query._mutable("_selects").append(selected)
        if self.query._groupbys:
            for group_term in term.get_group_by_terms():
                key = self.get_key(group_term)
                if key not in self.grouped_keys:
                    self.grouped_keys.add(key)
                    aliasless = group_term.as_(None) if group_term.alias is not None else group_term
                    self.query._mutable("_groupbys").append(aliasless)
        return column

    def get_reference(self, term: Term) -> Field:
        """A reference from the enclosing query to the projected operand."""
        return cast("Field", self.query[self.include(term).name])

    def get_output(self, aliases: Collection[str]) -> list[Field]:
        """The requested visible columns, preserving caller aliases and their order."""
        return [cast("Field", self.query[self.by_alias[alias].name]).as_(alias) for alias in aliases]
