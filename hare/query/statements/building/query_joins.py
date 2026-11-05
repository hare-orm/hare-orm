from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.enums import Lookup
from hare.query.expressions import F, Q
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.expression_result import TableCriterionTuple
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.lookup_info.lookup_paths import LookupPaths
from hare.query.statements.constants import NULL_REJECTING_LOOKUP_SUFFIXES
from hare.sql import JoinType, Table

if TYPE_CHECKING:
    from hare.models import Model
    from hare.query.statements.awaitable_query import AwaitableQuery


class QueryJoins:
    """The JOINs a query builds: a relation's table joined once per path, with the default scopes of
    the joined models folded into its condition, and which joins a filter, an ordering or a grouping
    needs."""

    @staticmethod
    def join_table_by_field(
        query: AwaitableQuery[Any],
        table: Table,
        related_field_name: str,
        related_field: RelationalField[Model],
        extra_condition: Q | None = None,
        value_wrapper_references: RecordedValueReferences | None = None,
    ) -> Table:
        """Joins the table of a relation's model to the query, scoped by the query's visibility.

        Args:
            query: The query.
            table: The table the relation starts at.
            related_field_name: The relation's name.
            related_field: The relation.
            extra_condition: A condition the JOIN adds (``Select(relation, extra_condition=...)``).
            value_wrapper_references: Receives the values' references while a plan is recorded.

        Returns:
            The joined table.
        """
        joins = LookupPaths.get_scoped_joins(
            table,
            related_field,
            related_field_name,
            visibility=query._visibility,
            extra_condition=extra_condition,
            dialect=query.dialect,
            connection=query._connection,
            value_wrapper_references=value_wrapper_references,
        )
        for join in joins:
            QueryJoins.join_table(query, join)
        return joins[-1][0]

    @staticmethod
    def reset_joined_tables(query: AwaitableQuery[Any]) -> None:
        """Clears the record of joined tables before the query is built again - a second build would
        take every join as already made.

        Args:
            query: The query.
        """
        query._joined_tables = []
        query._joined_tables_set = set()
        query._annotation_expression_results = {}
        query._expression_term_references = {}
        query._annotation_ordering_terms = {}

    @staticmethod
    def join_table(query: AwaitableQuery[Any], table_criterio_tuple: TableCriterionTuple) -> None:
        if table_criterio_tuple[0] not in query._joined_tables_set:
            query.query = query.query.join(table_criterio_tuple[0], how=JoinType.LEFT_OUTER).on(
                table_criterio_tuple[1]
            )
            query._joined_tables.append(table_criterio_tuple[0])
            query._joined_tables_set.add(table_criterio_tuple[0])

    @staticmethod
    def apply_effective_basetable(query: AwaitableQuery[Any]) -> None:
        """Aliases ``self.query``'s base table to ``_effective_basetable()`` - for a query built
        straight from the model's base query. A no-op unless a self-referential subquery needs the
        alias.

        Args:
            query: The query.
        """
        effective_table = query._effective_basetable()
        if effective_table != query.model._meta.basetable:
            query.query = query.query.replace_table(query.model._meta.basetable, effective_table)

    @staticmethod
    def get_field_lookups(query: AwaitableQuery[Any], field_names: Iterable[str]) -> list[str]:
        """The field lookup each name reads - itself, or the lookup of a plain ``F("a__b")``
        annotation.

        Args:
            query: The query.
            field_names: Field or annotation names.

        Returns:
            The lookups, other annotations left out.
        """
        lookups: list[str] = []
        for field_name in field_names:
            seen_names: set[str] = set()
            while field_name in query._annotations and field_name not in seen_names:
                seen_names.add(field_name)
                annotation = query._annotations[field_name]
                if type(annotation) is not F:
                    break
                field_name = annotation.name
            if field_name not in query._annotations:
                lookups.append(field_name)
        return lookups

    @staticmethod
    def get_ordering_lookups(query: AwaitableQuery[Any]) -> list[str]:
        """The effective ordering's field lookups, annotations left out.

        Args:
            query: The query.

        Returns:
            The lookups.
        """
        return [
            field_name
            for field_name, _order in query._apply_default_ordering(query._orderings)
            if field_name not in query._annotations and field_name not in query.model._meta.ordering_expressions
        ]

    @staticmethod
    def get_non_null_filtered_lookups(query: AwaitableQuery[Any]) -> set[str]:
        """The fields through a to-many relation that a null-rejecting filter (``tags__code="x"``,
        ``__isnull=False``, ``__in``) at the top of the first ``.filter()`` over the relation keeps
        from being NULL.

        Args:
            query: The query.

        Returns:
            The field lookups, without a lookup suffix.
        """
        conditions_by_generation = query._get_top_level_conditions_by_generation()
        first_generation_by_path: dict[str, int] = {}
        for q_object in query._q_objects:
            for key, _value in query._get_top_level_conditions(q_object, including_alternatives=True):
                for path in AggregatedMultiValuedPaths.get_multi_valued_paths(query.model, key):
                    first_generation = first_generation_by_path.get(path)
                    if first_generation is None or q_object._filter_call_generation < first_generation:
                        first_generation_by_path[path] = q_object._filter_call_generation
        non_null_lookups: set[str] = set()
        for generation, conditions in conditions_by_generation.items():
            for key, value in conditions:
                lookup = QueryJoins.get_null_rejected_lookup(key, value)
                if lookup is None:
                    continue
                paths = AggregatedMultiValuedPaths.get_multi_valued_paths(query.model, lookup)
                if paths and first_generation_by_path.get(paths[-1]) == generation:
                    non_null_lookups.add(lookup)
        return non_null_lookups

    @staticmethod
    def get_null_rejected_lookup(key: str, value: Any) -> str | None:
        """The field a filter kwarg keeps from being NULL.

        Args:
            key: The kwarg key.
            value: The kwarg value.

        Returns:
            The field lookup without its lookup suffix, or None when the kwarg can match NULL.
        """
        field_lookup, separator, suffix = key.rpartition("__")
        if not separator:
            field_lookup, suffix = key, ""
        if suffix == Lookup.ISNULL:
            return field_lookup if value is False else None
        if suffix == Lookup.NOT_ISNULL:
            return field_lookup if value is True else None
        if value is None:
            return None
        if (
            suffix == Lookup.IN
            and isinstance(value, (list, tuple, set, frozenset))
            and any(item is None for item in value)
        ):
            # ``__in=[..., None]`` matches NULL too (``OR ... IS NULL``).
            return None
        if suffix in NULL_REJECTING_LOOKUP_SUFFIXES:
            return field_lookup if suffix else key
        # The suffix is a field name itself (``tags__code="x"``).
        return key
