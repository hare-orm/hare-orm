from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import QueryError
from hare.fields.field import Field
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.enums import Connector, Lookup
from hare.query.expressions.conditions.query_modifier import QueryModifier
from hare.query.expressions.constants import TO_MANY_RELATION_SHORTCUT_LOOKUPS
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import TableCriterionTuple
from hare.query.expressions.subqueries.subquery import Subquery
from hare.query.expressions.value_references.composite_key_value_reference import CompositeKeyValueReference
from hare.query.expressions.value_references.row_list_value_reference import RowListValueReference
from hare.query.key_columns import KeyColumns
from hare.sql.terms.criteria.contains_criterion import ContainsCriterion
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term
from hare.sql.terms.tuple import Tuple
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.expressions.conditions.q import Q
    from hare.query.lookup_info.lookup_info import LookupInfo


class CompositeKeyFilters:
    """Filters on a key of several columns - a composite primary key or a relation to one: row
    comparisons, row lists, subqueries compared column by column."""

    @staticmethod
    def get_composite_relation_kwarg(
        condition: Q, expression_context: ExpressionContext, key: str, value: Any
    ) -> QueryModifier | None:
        """Resolves ``key=value`` naming a forward relation to a composite primary key as an AND of
        equalities on the relation's own key columns - no JOIN.

        Args:
            condition: The condition resolved.
            expression_context: Where the filter's terms resolve.
            key: The filter's keyword.
            value: The filter's value.

        Returns:
            The modifier, None for any other key.
        """
        # Local import: the filter resolution package is imported by Q's own module.
        from hare.query.expressions.conditions.q import Q

        model = expression_context.model
        relation_name, _, lookup = key.partition("__")
        if lookup not in TO_MANY_RELATION_SHORTCUT_LOOKUPS:
            return None
        if relation_name not in model._meta.foreign_key_fields and relation_name not in model._meta.one_to_one_fields:
            return None
        field_object = cast("RelationalField[Model]", model._meta.fields_map[relation_name])
        source_fields = tuple(field_object.source_fields)
        if len(source_fields) <= 1:
            return None
        if lookup in {"", "not"} and value is None:
            lookup, value = Lookup.ISNULL, lookup == Lookup.EXACT
        if lookup in {Lookup.ISNULL, Lookup.NOT_ISNULL}:
            # A composite key is null exactly when every key column is.
            is_null = bool(value) if lookup == Lookup.ISNULL else not bool(value)
            null_kwargs: list[dict[str, Any]] = [
                {f"{source_field}__isnull": is_null} for source_field in source_fields
            ]
            null_groups = [Q(**null_kwarg) for null_kwarg in null_kwargs]
            null_connector = Connector.AND if is_null else Connector.OR
            return Q.with_connector(null_connector, *null_groups).get_result(expression_context)
        # Local import: the queryset package imports this module.
        from hare.query.queryset.arguments.filter_arguments import FilterArguments

        if lookup in {Lookup.IN, Lookup.NOT_IN} and FilterArguments.is_subquery_filter_value(value):
            source_columns: list[Term] = [
                expression_context.table[model._meta.fields_db_projection[source_field]]
                for source_field in source_fields
            ]
            modifier = CompositeKeyFilters.get_composite_key_subquery_in_modifier(
                expression_context, value, source_columns, key
            )
            return ~modifier if lookup == Lookup.NOT_IN else modifier
        to_field_names = [to_field_instance.model_field_name for to_field_instance in field_object.to_field_instances]
        key_q = KeyColumns.get_comparison_q(
            key,
            source_fields,
            to_field_names,
            lookup,
            value,
            instance_key_names=to_field_names,
            reads_outer_references=True,
        )
        return key_q._with_filter_call_generation(condition._filter_call_generation).get_result(expression_context)

    @staticmethod
    def get_relation_key_value_reference(
        relation: RelationalField[Model], lookup_info: LookupInfo, value: Any, component_references: list[Any]
    ) -> CompositeKeyValueReference | None:
        """The reference a relation to a composite key binds a later key through - the references
        its key columns recorded, one per column of each key compared.

        Args:
            relation: The relation.
            lookup_info: The lookup on it.
            value: The value - an instance or a key tuple, or a list of them.
            component_references: The references recorded comparing the key columns.

        Returns:
            The reference, None when a value can't be bound - a column's comparison recorded none.
        """
        if not component_references or any(reference is None for reference in component_references):
            return None
        is_list = lookup_info.lookup in {Lookup.IN, Lookup.NOT_IN}
        if is_list and not isinstance(value, (list, tuple, set)):
            return None
        key_names = tuple(field.model_field_name for field in relation.to_field_instances)
        return CompositeKeyValueReference(tuple(component_references), key_names, relation.related_model, is_list)

    @staticmethod
    def get_composite_pk_kwarg(
        expression_context: ExpressionContext, key: str, value: Any
    ) -> tuple[QueryModifier, RowListValueReference | CompositeKeyValueReference | None] | None:
        """``"pk"``/``"pk__in"``/``"pk__not"``/``"pk__not_in"`` on a composite-pk model - several
        column conditions at once, built by ``KeyColumns.get_primary_key_q()``, as the same
        ``.filter()`` kwarg is.

        Args:
            expression_context: Where the filter's terms resolve.
            key: The filter's keyword.
            value: The filter's value.

        Returns:
            The modifier and the reference a later key binds through - a ``pk__in`` list's rows, a
            key's columns - or None for any other key.
        """
        composite_pk_q = KeyColumns.get_primary_key_q(expression_context.model, key, value)
        if composite_pk_q is None:
            return None
        if key == "pk__in" and composite_pk_q.filters.get(key) is not None:
            return CompositeKeyFilters.get_composite_pk_in_modifier(expression_context, composite_pk_q.filters[key])
        # The comparison of each key column records a reference of its own - the key binds through
        # them all at once, under this key.
        recording = expression_context.value_wrapper_references
        recorded_count = 0 if recording is None else len(recording)
        modifier = composite_pk_q.get_result(expression_context)
        if recording is None or key not in {"pk", "pk__not"} or not isinstance(value, tuple):
            return modifier, None
        component_references: list[Any] = [reference for _component_key, reference in recording[recorded_count:]]
        del recording[recorded_count:]
        if not component_references or any(reference is None for reference in component_references):
            return modifier, None
        model = expression_context.model
        return modifier, CompositeKeyValueReference(
            tuple(component_references), model._meta.primary_key_attribute_names, model, False
        )

    @staticmethod
    def get_composite_pk_in_modifier(
        expression_context: ExpressionContext, value_rows: list[tuple[Any, ...]]
    ) -> tuple[QueryModifier, RowListValueReference | None]:
        """``pk__in=`` on a composite-pk model as one row-value membership check -
        ``(a, b) IN ((1, 2), ...)``, bound as few parameters as the backend allows.

        Args:
            expression_context: The context the filter is resolved in.
            value_rows: The primary key tuples, already checked to match the key's shape.

        Returns:
            The modifier, and the reference a later query binds its rows through - None for rows
            not bound one parameter per value.
        """
        # Local import: the filter resolution package is imported by Q's own module.
        from hare.query.expressions.conditions.q import Q

        model = expression_context.model
        meta = model._meta
        primary_key_attribute = cast("tuple[str, ...]", meta.primary_key_attribute)
        # Local import: the queryset package imports this module.
        from hare.query.queryset.arguments.filter_arguments import FilterArguments

        if FilterArguments.is_subquery_filter_value(value_rows):
            subquery_modifier = CompositeKeyFilters.get_composite_key_subquery_in_modifier(
                expression_context,
                value_rows,
                CompositeKeyFilters.get_composite_pk_columns(expression_context),
                "pk__in",
            )
            return subquery_modifier, None
        # The key values column by column - each column is checked and converted in one pass.
        value_columns = (
            [list(value_column) for value_column in zip(*value_rows, strict=True)]
            if value_rows
            else [[] for _key_name in primary_key_attribute]
        )
        if any(CompositeKeyFilters.holds_expression(value_column) for value_column in value_columns):
            # An expression component has no bound value to put in a row list - one equality
            # group per row instead.
            groups = [Q(**dict(zip(primary_key_attribute, row, strict=True))) for row in value_rows]
            return Q.with_connector(Connector.OR, *groups).get_result(expression_context), None
        columns = CompositeKeyFilters.get_composite_pk_columns(expression_context)
        types = expression_context.dialect.types
        encoded_columns = [
            [None if value is None else types.get_lookup_value(pk_field, value, model) for value in value_column]
            if None in value_column
            else types.get_lookup_values(pk_field, value_column, model)
            for pk_field, value_column in zip(meta.pk_fields, value_columns, strict=True)
        ]
        has_none = any(None in encoded_column for encoded_column in encoded_columns)
        encoded_rows = list(zip(*encoded_columns, strict=True))
        criterion = expression_context.dialect.filter_operators.get_row_membership_criterion(
            columns, encoded_rows, list(meta.pk_fields)
        )
        modifier = QueryModifier(where_criterion=criterion, has_nullable_column=has_none)
        return modifier, None if has_none else CompositeKeyFilters.get_row_list_value_reference(
            criterion, encoded_rows, meta.pk_fields
        )

    @staticmethod
    def get_row_list_value_reference(
        criterion: Criterion, encoded_rows: list[tuple[Any, ...]], fields: Iterable[Field[Any]]
    ) -> RowListValueReference | None:
        """The reference of key rows compared one parameter per value - ``(a, b) IN ((?, ?), ...)``
        holding the encoded values themselves.

        Args:
            criterion: The row membership criterion.
            encoded_rows: The rows' encoded values.
            fields: The key's fields.

        Returns:
            The reference, None for another form (a container binding the rows at once).
        """
        if not isinstance(criterion, ContainsCriterion) or not isinstance(criterion.container, Tuple):
            return None
        row_terms = criterion.container.values
        if len(row_terms) != len(encoded_rows):
            return None
        for row_term, encoded_row in zip(row_terms, encoded_rows, strict=True):
            if not isinstance(row_term, Tuple) or len(row_term.values) != len(encoded_row):
                return None
            for component_term, component in zip(row_term.values, encoded_row, strict=True):
                if not isinstance(component_term, ValueWrapper) or component_term.value is not component:
                    return None
        return RowListValueReference(criterion.container, tuple(fields))

    @staticmethod
    def get_composite_pk_columns(expression_context: ExpressionContext) -> list[Term]:
        """The composite primary key columns of the resolved model, in key order.

        Args:
            expression_context: The context the filter is resolved in.

        Returns:
            The columns, each wrapped in its field's comparison cast where the dialect needs one.
        """
        meta = expression_context.model._meta
        dialect = expression_context.dialect
        columns: list[Term] = []
        for pk_field, column_name in zip(meta.pk_fields, KeyColumns.get_source_columns(meta), strict=True):
            column: Term = expression_context.table[column_name]
            if (function_cast := pk_field.get_function_cast(dialect)) is not None:
                column = function_cast(pk_field, column)
            columns.append(column)
        return columns

    @staticmethod
    def get_composite_key_subquery_in_modifier(
        expression_context: ExpressionContext, value: Any, columns: list[Term], key: str
    ) -> QueryModifier:
        """A composite key compared against a query - ``(a, b) IN (SELECT a, b ...)``.

        Args:
            expression_context: The context the filter is resolved in.
            value: A queryset (its rows' primary key), a union of such querysets, a ``KeyRowsQuery``, a
                ``values()``/``values_list()`` query or a ``Subquery`` selecting the key columns.
            columns: The compared key columns, in key order.
            key: The filter kwarg name, for the error message.

        Returns:
            The modifier.

        Raises:
            QueryError: The query doesn't select as many columns as the key has.
        """
        # Local import: Q's module imports this module, and the query statements import Q.
        from hare.query.expressions.subqueries.declarations import KeyRowsQuery
        from hare.query.queryset import QuerySet
        from hare.query.queryset.selection.statement_selection import StatementSelection
        from hare.query.statements.select.combined.combined_derived_queries import CombinedDerivedQueries
        from hare.query.statements.select.combined_query import CombinedQuery

        pk_column_count = len(columns)
        if isinstance(value, QuerySet):
            value = (
                value._get_compiler()
                if value._selection is not None or value._combination is not None
                else StatementSelection.get_primary_key_values_query(value)
            )
        joins: list[TableCriterionTuple] = []
        if isinstance(value, CombinedQuery) and not value._combines_values:
            subquery_term: Term = CombinedDerivedQueries.get_fields_values_query(
                value, value.model._meta.primary_key_attribute_names
            )
        elif isinstance(value, KeyRowsQuery):
            # It selects the key columns, in key order.
            recursive_rows_result = value.get_result(expression_context)
            subquery_term = recursive_rows_result.term
            joins = recursive_rows_result.joins
        else:
            subquery = value if isinstance(value, Subquery) else Subquery(value)
            selected_names = subquery.get_selected_names()
            if selected_names is not None and len(selected_names) != pk_column_count:
                raise QueryError(
                    f"'{key}' compares the {pk_column_count} columns of a composite key, but "
                    f"the subquery selects {len(selected_names)} ({', '.join(selected_names)})"
                )
            subquery_result = subquery.get_result(expression_context)
            subquery_term = subquery_result.term
            joins = subquery_result.joins
        criterion = Tuple(*columns).isin(subquery_term)
        return QueryModifier(where_criterion=criterion, joins=joins)

    @staticmethod
    def holds_expression(values: list[Any]) -> bool:
        """Whether one of ``values`` is an expression or a SQL term - checked by the values' types."""
        return any(issubclass(value_type, (Expression, Term)) for value_type in set(map(type, values)))

    @staticmethod
    def get_filter_column_count(model: type[Model], key: str) -> int:
        """The number of columns a filter kwarg compares - more than one for a composite primary
        key or a composite relation.

        Args:
            model: The model the kwarg is resolved against.
            key: The filter kwarg.

        Returns:
            The column count.
        """
        base_field_name = key.partition("__")[0]
        meta = model._meta
        if base_field_name == "pk":
            return len(meta.primary_key_attribute_names)
        field_object = meta.fields_map.get(base_field_name)
        if base_field_name in meta.foreign_key_fields or base_field_name in meta.one_to_one_fields:
            return max(len(getattr(field_object, "source_fields", ()) or ()), 1)
        if (
            base_field_name in meta.many_to_many_fields
            or base_field_name in meta.backward_foreign_key_fields
            or base_field_name in meta.backward_one_to_one_fields
        ):
            related_model = cast("RelationalField[Model]", field_object).related_model
            return len(related_model._meta.primary_key_attribute_names)
        return 1
