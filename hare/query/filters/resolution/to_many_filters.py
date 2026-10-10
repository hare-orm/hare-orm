from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import UnSupportedError
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.query.enums import Lookup
from hare.query.expressions.conditions.query_modifier import QueryModifier
from hare.query.expressions.constants import MANY_TO_MANY_EXISTS_LOOKUPS, TO_MANY_RELATION_SHORTCUT_LOOKUPS
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import TableCriterionTuple
from hare.query.expressions.value_references.related_key_value_reference import RelatedKeyValueReference
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.filters.resolution.filter_values import FilterValues
from hare.query.filters.resolution.relation_filters import RelationFilters
from hare.query.key_columns import KeyColumns
from hare.query.lookup_info.lookup_paths import LookupPaths
from hare.query.plans.description.plannable import Plannable
from hare.sql import JoinType, Table
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.identifiers import Identifiers
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.criteria.not_criterion import Not
from hare.sql.terms.star import Star
from hare.sql.terms.subqueries.exists_term import ExistsTerm
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.conditions.q import Q
    from hare.query.filters.lookups.field_lookup import FieldLookup


class ToManyFilters:
    """Filters through a to-many relation: the isnull test of a many-to-many relation, the visible
    target of its through table, the shortcut of a relation compared with an object, and a negation
    across JOINs as NOT EXISTS."""

    @staticmethod
    def get_many_to_many_isnull_kwarg(
        expression_context: ExpressionContext, key: str, value: Any, table: Table
    ) -> QueryModifier | None:
        """Resolves ``<m2m>__isnull``/``<m2m>__not_isnull`` into a correlated ``[NOT] EXISTS`` over the
        through table joined to the related model, each under its default scope.

        Args:
            expression_context: The context the filter is resolved in.
            key: The filter key.
            value: The filter value.
            table: The table the filtered model is read through.

        Returns:
            The modifier, None if ``key`` isn't such a lookup on a many-to-many field.

        Raises:
            UnSupportedError: ``value`` isn't a bool.
        """
        relation_name, _, lookup = key.partition("__")
        if lookup not in {Lookup.ISNULL, Lookup.NOT_ISNULL}:
            return None
        many_to_many_field = expression_context.model._meta.fields_map.get(relation_name)
        if not isinstance(many_to_many_field, ManyToManyFieldInstance):
            return None
        if not isinstance(value, bool):
            raise UnSupportedError(f"__{lookup} expects a bool, got {value!r}")
        joins = LookupPaths.get_scoped_joins(
            table,
            many_to_many_field,
            relation_name,
            visibility=expression_context.visibility,
            dialect=expression_context.dialect,
            connection=expression_context.connection,
        )
        (through_table, correlation), (related_table, related_criterion) = joins
        inner_query = (
            QueryBuilder()
            .from_(through_table)
            .join(related_table, how=JoinType.INNER)
            .on(related_criterion)
            .select(Star())
            .where(correlation)
        )
        is_empty = value if lookup == Lookup.ISNULL else not value
        exists_criterion = ExistsTerm(inner_query)
        return QueryModifier(where_criterion=Not(exists_criterion) if is_empty else exists_criterion)

    @staticmethod
    def join_visible_many_to_many_target(
        expression_context: ExpressionContext,
        many_to_many_field: ManyToManyFieldInstance[Any],
        relation_name: str,
        joins: list[TableCriterionTuple],
        table: Table,
    ) -> Criterion | None:
        """Appends to ``joins`` a join from the through table to the related model under its default
        scope, so a bare relation filter counts only the links to visible rows.

        Args:
            expression_context: The context the filter is resolved in.
            many_to_many_field: The many-to-many field the filter names.
            relation_name: The field's name on the filtered model.
            joins: The filter's joins, holding the through-table join; changed in place.
            table: The table the filtered model is read through.

        Returns:
            A criterion holding only when the linked row is visible, None if the related model has
            no default scope.
        """
        from hare.query.scopes.row_scopes import RowScopes

        related_meta = many_to_many_field.related_model._meta
        target_table = related_meta.basetable.as_(
            Identifiers.get_within_limit(f"{table.get_table_name()}__{relation_name}__visible")
        )
        visible_target_criterion = RowScopes.of(many_to_many_field.related_model).get_criterion(
            target_table,
            visibility=expression_context.visibility,
            dialect=expression_context.dialect,
            connection=expression_context.connection,
        )
        if visible_target_criterion is None:
            return None
        through_table = joins[0][0]
        target_pk_columns = KeyColumns.get_source_columns(related_meta)
        joins.append(
            (
                target_table,
                KeyColumns.row_equality(
                    [through_table[column] for column in many_to_many_field.forward_keys],
                    [target_table[column] for column in target_pk_columns],
                )
                & visible_target_criterion,
            )
        )
        return target_table[target_pk_columns[0]].notnull()

    @staticmethod
    def get_to_many_relation_shortcut_kwarg(
        condition: Q, expression_context: ExpressionContext, key: str, value: Any
    ) -> tuple[QueryModifier, RelatedKeyValueReference | None, bool] | None:
        """Resolves a lookup on a to-many relation's own name (``tags=obj``, ``tags__in=[...]``,
        ``emps__isnull=True``) as the same lookup on the related primary key, joined like the other
        lookups through the relation. ``relation=None`` is ``relation__isnull=True``; a reverse
        relation to a composite primary key compares every key column.

        Args:
            condition: The condition resolved.
            expression_context: The context the filter is resolved in.
            key: The filter key.
            value: The filter value.

        Returns:
            The modifier, the reference a later query binds its value through (None when it can't)
            and whether an expression value recorded its own references instead; None if ``key``
            isn't such a lookup, or compares composite primary key values of a many-to-many
            relation.

        Raises:
            QueryError: A composite primary key value isn't a model instance or a tuple of the key's
                length.
        """
        from hare.models import Model
        from hare.query.expressions.conditions.q import Q

        # Local import: Q's module and the keyword filters import this module.
        from hare.query.expressions.subqueries.outer_reference import OuterReference

        relation_name, _, lookup = key.partition("__")
        if lookup not in TO_MANY_RELATION_SHORTCUT_LOOKUPS:
            return None
        relation_field = expression_context.model._meta.fields_map.get(relation_name)
        if not isinstance(relation_field, (BackwardForeignKeyRelation, ManyToManyFieldInstance)):
            return None
        if lookup in {"", "not"} and value is None:
            # Like Django, `relation=None` keeps the rows with no related row at all.
            isnull_kwarg: dict[str, Any] = {f"{relation_name}__isnull": lookup == Lookup.EXACT}
            isnull_q = Q(**isnull_kwarg)
            isnull_q._filter_call_generation = condition._filter_call_generation
            return (
                isnull_q.get_result(dataclasses.replace(expression_context, value_wrapper_references=None)),
                None,
                False,
            )
        is_many_to_many = isinstance(relation_field, ManyToManyFieldInstance)
        target_primary_key_attribute_names = relation_field.related_model._meta.primary_key_attribute_names
        if (
            not is_many_to_many
            and len(target_primary_key_attribute_names) > 1
            and lookup not in {Lookup.ISNULL, Lookup.NOT_ISNULL}
        ):
            composite_q = ToManyFilters.get_related_key_comparison(
                key, relation_name, target_primary_key_attribute_names, lookup, value
            )._with_filter_call_generation(condition._filter_call_generation)
            return (
                composite_q.get_result(dataclasses.replace(expression_context, value_wrapper_references=None)),
                None,
                False,
            )
        if not relation_field.is_multi_valued:
            return None
        if is_many_to_many and lookup in MANY_TO_MANY_EXISTS_LOOKUPS:
            return None
        if (
            is_many_to_many
            and len(target_primary_key_attribute_names) > 1
            and lookup in {Lookup.EXACT, Lookup.NOT}
            and isinstance(value, OuterReference)
        ):
            # The outer row's key, component by component - the through table's link is no single
            # column. Like a single-column key, `__not` keeps a row linked to another row or to none.
            outer_key_q = ToManyFilters.get_related_key_comparison(
                key, relation_name, target_primary_key_attribute_names, lookup, value
            )
            if lookup == Lookup.NOT:
                no_link_kwarg: dict[str, Any] = {f"{relation_name}__isnull": True}
                outer_key_q = outer_key_q | Q(**no_link_kwarg)
            outer_key_q = outer_key_q._with_filter_call_generation(condition._filter_call_generation)
            # Its comparisons read the outer row and a constant - they record their own (no) values.
            return outer_key_q.get_result(expression_context), None, True
        given_value = value
        converts_lists = lookup in {Lookup.IN, Lookup.NOT_IN}
        if isinstance(value, Model):
            value = value.pk
        elif converts_lists and isinstance(value, (list, tuple, set)):
            value = [element.pk if isinstance(element, Model) else element for element in value]
        # A plain value (not a query or an expression) binds in a plan, through its key.
        is_plain_value = not isinstance(given_value, (Plannable, Term))
        relation_path = RelationFilters.get_relation_path(expression_context, relation_name)
        if isinstance(relation_field, ManyToManyFieldInstance) and relation_path not in (
            expression_context.select_related_extra_conditions or {}
        ):
            # The through table's link column is the related primary key - and its JOIN already
            # holds only links to rows the related model's default scope shows - so, like Django,
            # the related table itself isn't joined.
            modifier, through_reference = ToManyFilters.get_through_table_kwarg(
                condition,
                expression_context,
                key,
                value,
                relation_field,
                reads_plain_value=is_plain_value,
                converts_lists=converts_lists,
            )
            # An expression value recorded its own references while it was resolved
            # (FilterValues.get_filter_value()).
            return modifier, through_reference, isinstance(given_value, Expression)
        if len(target_primary_key_attribute_names) > 1 and lookup not in {Lookup.ISNULL, Lookup.NOT_ISNULL}:
            return None
        # For IS [NOT] NULL: the LEFT JOIN found no row exactly when any one of its primary key
        # columns is NULL - or, for a related model without a primary key, its key column to
        # this row.
        target_meta = relation_field.related_model._meta
        target_column_name = (
            target_primary_key_attribute_names[0]
            if target_meta.has_primary_key
            else target_meta.fields_db_projection_reverse[
                cast("BackwardForeignKeyRelation[Any]", relation_field).relation_source_fields[0]
            ]
        )
        nested_key = f"{relation_name}__{target_column_name}"
        if lookup:
            nested_key = f"{nested_key}__{lookup}"
        nested_q = Q(**{nested_key: value})
        nested_q._filter_call_generation = condition._filter_call_generation
        nested_references: RecordedValueReferences | None = (
            [] if is_plain_value and expression_context.value_wrapper_references is not None else None
        )
        modifier = nested_q.get_result(
            dataclasses.replace(expression_context, value_wrapper_references=nested_references)
        )
        nested_reference = None
        if nested_references is not None and len(nested_references) == 1 and nested_references[0][1] is not None:
            nested_reference = RelatedKeyValueReference(nested_references[0][1], Model, converts_lists)
        return modifier, nested_reference, False

    @staticmethod
    def get_related_key_comparison(
        key: str, relation_name: str, target_key_names: tuple[str, ...], lookup: str, value: Any
    ) -> Q:
        """A lookup on a to-many relation to a composite key, as the comparison of every key column.

        Args:
            key: The filter key.
            relation_name: The relation.
            target_key_names: The fields of the related primary key.
            lookup: The lookup.
            value: The filter value.

        Returns:
            The condition.

        Raises:
            QueryError: The value isn't a model instance or a tuple of the key's length.
        """
        return KeyColumns.get_comparison_q(
            key,
            tuple(f"{relation_name}__{key_name}" for key_name in target_key_names),
            target_key_names,
            lookup,
            value,
            reads_outer_references=True,
        )

    @staticmethod
    def get_through_table_kwarg(
        condition: Q,
        expression_context: ExpressionContext,
        key: str,
        value: Any,
        relation_field: ManyToManyFieldInstance[Any],
        *,
        reads_plain_value: bool,
        converts_lists: bool,
    ) -> tuple[QueryModifier, RelatedKeyValueReference | None]:
        """A lookup on a many-to-many relation's own name compared with the through table's link
        column.

        Args:
            condition: The condition resolved.
            expression_context: The context the filter is resolved in.
            key: The filter key.
            value: The filter value - a model instance already replaced by its key.
            relation_field: The relation.
            reads_plain_value: Whether the value given is a plain value, not a query or an expression.
            converts_lists: Whether a list's model instances were replaced by their keys.

        Returns:
            The modifier, and the reference a later query binds its value through - None when it
            can't.
        """
        from hare.models import Model

        # Local import: the keyword filters import this module.
        from hare.query.filters.resolution.filter_kwargs import FilterKwargs

        relation_name = key.partition("__")[0]
        lookup_info = FilterKwargs.get_key_lookup_info(expression_context, key)
        filter_value, value_joins, __ = FilterValues.get_filter_value(expression_context, key, value)
        _, _, relation_joins = RelationFilters.get_relation_joins(
            condition, expression_context, relation_name, expression_context.table
        )
        through_join = relation_joins[0]
        criterion, _, _, _, _, operator, encoded_value, _ = FilterKwargs.process_filter_kwarg(
            expression_context, lookup_info, filter_value, expression_context.table, join_table=through_join[0]
        )
        modifier = QueryModifier(joins=value_joins) & QueryModifier(where_criterion=criterion, joins=[through_join])
        if not reads_plain_value or filter_value is not value:
            return modifier, None
        value_reference = FilterValues.get_value_reference(
            expression_context,
            criterion,
            operator,
            filter_value,
            encoded_value,
            relation_field,
            cast("FieldLookup", lookup_info.field_lookup).value_encoder,
        )
        if value_reference is None:
            return modifier, None
        return modifier, RelatedKeyValueReference(value_reference, Model, converts_lists)

    @staticmethod
    def negate_across_joins(modifier: QueryModifier, expression_context: ExpressionContext) -> QueryModifier:
        """Negates a filter that crossed a relation as a correlated ``NOT EXISTS`` subquery: ``NOT
        (criterion)`` over a LEFT JOIN is UNKNOWN for a row with no related row and would drop it.
        The subquery reuses the joins and criterion already built, retargeted onto an aliased copy
        of the base table correlated by primary key (by every column for a model without one).

        Args:
            modifier: The filter negated.
            expression_context: Where the filter's terms resolve.
        """
        outer_table = expression_context.table
        negation_depth = modifier.negation_depth + 1
        depth_suffix = "" if negation_depth == 1 else str(negation_depth)
        inner_table = outer_table.as_(
            Identifiers.get_within_limit(f"{outer_table.get_table_name()}__negated{depth_suffix}")
        )
        inner_where = modifier.where_criterion.replace_table(outer_table, inner_table)
        correlation = KeyColumns.get_row_correlation(expression_context.model._meta, inner_table, outer_table)
        inner_query = QueryBuilder().from_(inner_table).select(Star())
        # Children crossing the same relation path each contribute the same join - it is joined
        # once, by join table.
        seen_join_tables: set[Table] = set()
        for join_table, join_criterion in modifier.joins:
            if join_table in seen_join_tables:
                continue
            seen_join_tables.add(join_table)
            inner_query = inner_query.join(join_table, how=JoinType.LEFT_OUTER).on(
                join_criterion.replace_table(outer_table, inner_table)
            )
        inner_query = inner_query.where(correlation & inner_where)
        return QueryModifier(where_criterion=Not(ExistsTerm(inner_query)), negation_depth=negation_depth)
