from __future__ import annotations

from collections.abc import Collection, Mapping
from copy import copy
from typing import TYPE_CHECKING, Any

from hare.query.statements.constants import DISTINCT_ORDERING_COLUMN_ALIAS_PREFIX, RESULT_COLUMN_ALIAS_PREFIX
from hare.query.statements.projections.declarations import ProjectionAliases, ProjectionColumn
from hare.sql.builder.queries.set_operation_query import SetOperationQuery
from hare.sql.identifiers import Identifiers
from hare.sql.terms.field import Field
from hare.sql.terms.node_structure import NodeStructure
from hare.sql.terms.select_reference import SelectReference

if TYPE_CHECKING:
    from hare.sql.builder.queries.query_builder import QueryBuilder
    from hare.sql.terms.term import Term


class DerivedProjection:
    """Columns made available to a query outside a prepared SELECT.

    Structural identities index the selected terms and their SQL names. The enclosing query
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
        column_aliases: Mapping[int, str] | None = None,
    ) -> None:
        if isinstance(query, SetOperationQuery):
            source = copy(query)
            with_clauses, source._with = source._with, []
            names = [term.alias or term.name for term in source.base_query._selects]  # type: ignore[attr-defined]
            query = source.base_query.query_class.from_(source).select(*(source[name].as_(name) for name in names))
            query._with = with_clauses
        self.query = copy(query)
        self.column_prefix = column_prefix
        self.columns: list[ProjectionColumn] = []
        self.by_key: dict[tuple[Any, ...], ProjectionColumn] = {}
        self.by_alias: dict[str, ProjectionColumn] = {}
        self.names = Identifiers()
        # Canonical sources of this statement, before all table references use binding identity.
        self.source_bindings = NodeStructure.get_source_bindings(query)
        selects = self.query._mutable("_selects")
        for position, term in enumerate(selects):
            alias = self.get_output_name(term)
            name = self.get_free_name(position) if column_prefix is not None or alias is None else alias
            if column_aliases is not None and position in column_aliases:
                name = column_aliases[position]
            self.names.add(name)
            selected = term.as_(name) if column_prefix is not None or name != alias else term
            key = self.get_key(term)
            column = ProjectionColumn(name, selected)
            self.columns.append(column)
            self.by_key.setdefault(key, column)
            if alias is not None:
                self.by_alias.setdefault(alias, column)
            selects[position] = selected
        groupbys = self.query._mutable("_groupbys")
        for position, term in enumerate(groupbys):
            groupbys[position] = self.get_selected_reference(term)
        orderbys = self.query._mutable("_orderbys")
        for position, (term, order) in enumerate(orderbys):
            orderbys[position] = (self.get_selected_reference(term), order)
        self.grouped_keys = {self.get_key(term) for term in self.query._groupbys}

    @staticmethod
    def get_output_name(term: Term) -> str | None:
        """The alias or field name of one selected term, if it has one."""
        return term.alias or (term.name if isinstance(term, Field) else None)

    def get_selected_reference(self, term: Term) -> Term:
        """Keep a GROUP BY or ORDER BY alias bound to its column after internal renaming."""
        if (
            isinstance(term, SelectReference)
            or isinstance(term, Field)
            and term.table is None
            and term.name in self.by_alias
        ) and (column := self.find(term)) is not None:
            return SelectReference(column.term)
        return term

    @classmethod
    def protect_column_names(cls, query: QueryBuilder, source_names: Collection[str] = ()) -> ProjectionAliases:
        """Give conflicting outputs private SQL names while retaining their original reader names.

        Some SQL engines expand a selected alias inside other expressions, hiding physical columns.
        Duplicate or overlong labels are also unsafe for name-based readers. Identity projections
        keep their names; computed and renamed outputs use the same identifier allocator as derived
        columns. Clause references follow their expressions, not a rendered SQL replacement.
        """
        if not any(term.alias is not None for term in query._selects):
            return ProjectionAliases(query)
        source_names = {*source_names, *(node.name for node in query.nodes_() if isinstance(node, Field))}
        folded_sources = {name.casefold() for name in source_names}
        names = Identifiers(source_names | {term.alias for term in query._selects if term.alias is not None})
        original_names = tuple(cls.get_output_name(term) for term in query._selects)
        seen_names: set[str | None] = set()
        column_aliases: dict[int, str] = {}
        output_aliases: dict[str, str] = {}
        for position, term in enumerate(query._selects):
            alias = term.alias
            if alias is not None and (
                alias in seen_names
                or Identifiers.get_within_limit(alias) != alias
                or alias.casefold() in folded_sources
                and not (isinstance(term, Field) and term.name == alias)
            ):
                name = names.get_free_name(f"{RESULT_COLUMN_ALIAS_PREFIX}{position}")
                names.add(name)
                column_aliases[position] = name
                output_aliases[alias] = name
            seen_names.add(original_names[position])
        if not column_aliases:
            return ProjectionAliases(query)
        return ProjectionAliases(cls(query, column_aliases=column_aliases).query, output_aliases, original_names)

    def get_key(self, term: Term) -> tuple[Any, ...]:
        """The computed value, independent of a selected alias or GROUP BY position."""
        if isinstance(term, SelectReference):
            term = term.term
        return NodeStructure.get_key(term, self.source_bindings)

    def get_free_name(self, position: int) -> str:
        """Allocate a column name without collisions under case-insensitive identifiers."""
        prefix = self.column_prefix or DISTINCT_ORDERING_COLUMN_ALIAS_PREFIX
        return self.names.get_free_name(f"{prefix}{position}")

    def find(self, term: Term) -> ProjectionColumn | None:
        """Find a selected alias reference or a structurally equal bound value."""
        if isinstance(term, Field) and term.table is None and term.name in self.by_alias:
            return self.by_alias[term.name]
        return self.by_key.get(self.get_key(term))

    def include(self, term: Term) -> ProjectionColumn:
        """Expose an operand as a hidden column and add any required grouping inputs."""
        if (column := self.find(term)) is not None:
            return column
        name = self.get_output_name(term)
        if self.column_prefix is not None or name is None or name in self.names:
            name = self.get_free_name(len(self.columns))
        self.names.add(name)
        selected = term.as_(name)
        key = self.get_key(term)
        column = ProjectionColumn(name, selected)
        self.columns.append(column)
        self.by_key[key] = column
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
        return self.query[self.include(term).name]  # type: ignore[return-value]

    def get_output(self, aliases: Collection[str]) -> list[Field]:
        """The requested visible columns, preserving caller aliases and their order."""
        return [self.query[self.by_alias[alias].name].as_(alias) for alias in aliases]  # type: ignore[misc]

    @staticmethod
    def get_selects_in_output_order(branch_query: QueryBuilder, aliases: list[str]) -> QueryBuilder:
        """A statement with visible cells in its public positional order - an embedded query
        pairs columns by position, and ``values()`` selects the model's fields before its
        annotations, whatever order the names put them in.

        Args:
            branch_query: The branch's statement.
            aliases: The aliases of its public columns in output order. Repeated actual aliases
                use their first occurrence; all other cells retain their original relative order.

        Returns:
            The statement - a copy with its columns reordered, or itself when they are in order.
        """
        selects = branch_query._selects
        current_aliases = [DerivedProjection.get_output_name(select) for select in selects]
        if current_aliases[: len(aliases)] == aliases:
            return branch_query
        first_positions: dict[str | None, int] = {}
        for position, alias in enumerate(current_aliases):
            first_positions.setdefault(alias, position)
        if not set(aliases) <= first_positions.keys():
            return branch_query
        public_positions = [first_positions[alias] for alias in aliases]
        public_position_set = set(public_positions)
        ordered_query = copy(branch_query)
        ordered_query._selects = [
            *(selects[position] for position in public_positions),
            *(select for position, select in enumerate(selects) if position not in public_position_set),
        ]
        return ordered_query
