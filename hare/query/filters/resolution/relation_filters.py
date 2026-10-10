from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import QueryError
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.fields.relations.relation_values import RelationValues
from hare.query.enums import Lookup, LookupTarget
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.conditions.query_modifier import QueryModifier
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import TableCriterionTuple
from hare.query.expressions.value_references.related_value_reference import RelatedValueReference
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.key_columns import KeyColumns
from hare.query.lookup_info.lookup_paths import LookupPaths
from hare.sql import Table
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.identifiers import Identifiers
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.criteria.not_criterion import Not
from hare.sql.terms.star import Star
from hare.sql.terms.subqueries.exists_term import ExistsTerm
from hare.sql.terms.term import Term
from hare.sql.terms.tuple import Tuple

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.expressions.conditions.q import Q
    from hare.query.filters.lookups.field_lookup import FieldLookup
    from hare.query.lookup_info.lookup_info import LookupInfo


class RelationFilters:
    """Filters across a relation: the path to the related model, the JOINs it takes, the nested filter
    resolved in the related model's context, and the key comparisons of a forward relation."""

    @staticmethod
    def get_relation_path(expression_context: ExpressionContext, related_field_name: str) -> str:
        """The path of a relation from the queried model.

        Args:
            expression_context: The context the filter is resolved in.
            related_field_name: The relation's name on the filtered model.

        Returns:
            The ``__``-separated path.
        """
        if expression_context.select_related_path_prefix:
            return f"{expression_context.select_related_path_prefix}__{related_field_name}"
        return related_field_name

    @staticmethod
    def get_relation_joins(
        condition: Q, expression_context: ExpressionContext, related_field_name: str, table: Table
    ) -> tuple[RelationalField[Model], str, list[TableCriterionTuple]]:
        """The JOINs a filter through a relation adds - a separate JOIN of a to-many relation for
        each ``.filter()``/``.exclude()`` call, scoped by the related model's default scope and a
        ``Select(relation, extra_condition=...)`` of the same path.

        Args:
            condition: The condition resolved.
            expression_context: The context the filter is resolved in.
            related_field_name: The relation's name on the filtered model.
            table: The table the filtered model is read through.

        Returns:
            The relation, its path from the queried model and the JOINs, the related table's last.

        Raises:
            QueryError: An aggregate over the same to-many relation would see only one of
                several JOINs, or an extra condition reads a further relation.
        """
        related_field = cast("RelationalField[Model]", expression_context.model._meta.fields_map[related_field_name])
        full_path = RelationFilters.get_relation_path(expression_context, related_field_name)
        # The first generation to touch a to-many path takes the unsuffixed alias; only a later,
        # different generation gets a JOIN of its own.
        effective_filter_call_generation = 0
        if (
            related_field.is_multi_valued
            and condition._filter_call_generation
            and expression_context.multi_valued_join_generations is not None
        ):
            seen_generation = expression_context.multi_valued_join_generations.get(full_path)
            if seen_generation is None or seen_generation == condition._filter_call_generation:
                expression_context.multi_valued_join_generations[full_path] = condition._filter_call_generation
            else:
                effective_filter_call_generation = condition._filter_call_generation
                # An aggregate that crossed this relation reads only one of the JOINs, while the
                # WHERE spans both - its value would be wrong, so this raises.
                aggregated_paths = expression_context.aggregated_multi_valued_paths
                if aggregated_paths and full_path in aggregated_paths:
                    raise QueryError(
                        f"Combining an aggregate (Count/Sum/Avg/...) over '{full_path}' with two or "
                        "more separate .filter()/.exclude() calls on that same to-many relation produces an "
                        "ambiguous result - the aggregate only sees one of the resulting JOINs. Combine the "
                        f"filters into a single call (e.g. .filter({full_path}__a=x, "
                        f"{full_path}__b=y) or Q(...) & Q(...)) instead, or move the aggregate into "
                        "a separate query."
                    )
        tracker = AggregatedMultiValuedPaths.get_from(expression_context)
        if related_field.is_multi_valued and tracker is not None:
            tracker.record_path(full_path, effective_filter_call_generation)
        # A `Select(relation, extra_condition=Q(...))` of the same relation is folded into this
        # JOIN, not left for SelectRelatedJoins.join_select_related() - the first JOIN added for a table wins.
        required_joins = LookupPaths.get_scoped_joins(
            table,
            related_field,
            related_field_name,
            visibility=expression_context.visibility,
            extra_condition=(
                expression_context.select_related_extra_conditions.get(full_path)
                if expression_context.select_related_extra_conditions
                else None
            ),
            filter_call_generation=effective_filter_call_generation,
            dialect=expression_context.dialect,
            connection=expression_context.connection,
        )
        return related_field, full_path, required_joins

    @staticmethod
    def get_nested_filter(
        condition: Q, expression_context: ExpressionContext, key: str, value: Any, table: Table
    ) -> tuple[QueryModifier, RelatedValueReference | None]:
        """Resolves a ``related__field=value`` kwarg by resolving ``Q(**{forwarded_fields: value})``
        against the related model. When a plan is recorded, the nested filter's value reference is
        folded into one ``RelatedValueReference`` under the caller's key - None when it recorded nothing
        bindable.

        Args:
            condition: The condition resolved.
            expression_context: Where the filter's terms resolve.
            key: The filter's keyword.
            value: The filter's value.
            table: The filtered model's table.
        """
        # Local import: the filter resolution package is imported by Q's own module.
        from hare.query.expressions.conditions.q import Q

        related_field_name, __, forwarded_fields = key.partition("__")
        related_field, full_path, required_joins = RelationFilters.get_relation_joins(
            condition, expression_context, related_field_name, table
        )
        nested_condition = Q(**{forwarded_fields: value})
        # The generation goes one hop deeper, for a multi-hop path.
        nested_condition._filter_call_generation = condition._filter_call_generation
        # Typed identically to ExpressionContext.value_wrapper_references itself (list is invariant, so a
        # narrower element type here would mismatch what ExpressionContext expects below).
        nested_references: RecordedValueReferences | None = (
            [] if expression_context.value_wrapper_references is not None else None
        )
        modifier = nested_condition.get_result(
            ExpressionContext(
                model=related_field.related_model,
                dialect=expression_context.dialect,
                connection=expression_context.connection,
                table=required_joins[-1][0],
                annotations=expression_context.annotations,
                value_wrapper_references=nested_references,
                # Passed on, so a further hop matches an extra_condition registered under the full
                # path.
                select_related_extra_conditions=expression_context.select_related_extra_conditions,
                select_related_path_prefix=full_path,
                # Forwarded so a further to-many hop of this same lookup is recorded too, and gets
                # a separate JOIN per .filter() call like a first hop does.
                aggregated_multi_valued_paths=expression_context.aggregated_multi_valued_paths,
                multi_valued_join_generations=expression_context.multi_valued_join_generations,
                visibility=expression_context.visibility,
            )
        )
        reference: RelatedValueReference | None = None
        if nested_references is not None and len(nested_references) == 1:
            _nested_key, nested_reference = nested_references[0]
            if isinstance(nested_reference, RelatedValueReference):
                # A multi-hop filter - converted for the model at its far end.
                reference = nested_reference
            elif nested_reference is not None:
                reference = RelatedValueReference(nested_reference, related_field.related_model)
        return QueryModifier(joins=required_joins) & modifier, reference

    @staticmethod
    def get_relation_lookup_criterion(
        expression_context: ExpressionContext,
        lookup_info: LookupInfo,
        value: Any,
        table: Table,
        join_table: Table | None,
    ) -> tuple[Criterion, TableCriterionTuple, Callable[..., Any], Any]:
        """Builds the criterion of a lookup on a many-to-many or backward relation itself - the
        related rows' key compared on the through table or the related table, joined by the key
        columns (a row of them for a composite key).

        Args:
            expression_context: The context the filter is resolved in.
            lookup_info: The filter key's description.
            value: The filter value.
            table: The table the filtered model is read through.
            join_table: The through or related table, when it is joined already.

        Returns:
            The criterion, the join, the operator and the converted value.
        """
        model = expression_context.model
        relation = lookup_info.relations[-1]
        field_lookup = cast("FieldLookup", lookup_info.field_lookup)
        compared_term: Term
        if isinstance(relation, ManyToManyFieldInstance):
            related_table = (
                join_table if join_table is not None else Table(relation.through, schema=relation.through_schema)
            )
            left_columns = KeyColumns.get_source_columns(model._meta)
            right_columns = relation.backward_keys
            compared_term = (
                Tuple(*[related_table[column] for column in relation.forward_keys])
                if relation.related_model._meta.pk is None
                else related_table[relation.forward_key]
            )
        else:
            backward_relation = cast("BackwardForeignKeyRelation[Model]", relation)
            owner_meta = backward_relation.related_model._meta
            related_table = (
                join_table
                if join_table is not None
                else RelationFilters.get_filter_join_table(
                    model, backward_relation.model_field_name, table, Table(owner_meta.db_table)
                )
            )
            left_columns = tuple(
                target_field.source_field or target_field.model_field_name
                for target_field in backward_relation.to_field_instances
            )
            right_columns = backward_relation.relation_source_fields
            # A LEFT JOIN that found no row nulls every joined column, so a model without a
            # primary key is tested by its key column to this row - never NULL in a joined row.
            compared_term = related_table[
                KeyColumns.get_source_columns(owner_meta)[0]
                if owner_meta.has_primary_key
                else backward_relation.relation_source_fields[0]
            ]
        join = (
            related_table,
            KeyColumns.row_equality(
                [table[column] for column in left_columns], [related_table[column] for column in right_columns]
            ),
        )
        if field_lookup.value_encoder is not None and not isinstance(value, Term):
            # A value that is a SQL term (OuterReference/Subquery/F()) is compared as it is.
            value = field_lookup.value_encoder(value, model, relation, expression_context.dialect)
        operator = expression_context.dialect.filter_operators.get_operator(field_lookup)
        return operator(compared_term, value), join, operator, value

    @staticmethod
    def get_related_instance_key_value(key: str, field_object: RelationalField[Model], obj: Model) -> Any:
        """The value a forward relation's key column holds for a related obj - its ``to_field``.

        Args:
            key: The filter kwarg name.
            field_object: The forward FK/O2O field.
            obj: The related obj.

        Returns:
            The key value.

        Raises:
            QueryError: The obj isn't of the related model, or is unsaved.
        """
        related_model = field_object.related_model
        if not isinstance(obj, related_model):
            raise QueryError(
                f"'{key}' expects {related_model.__name__} instances or key values, got a "
                f"{type(obj).__name__} instance"
            )
        (key_value,) = RelationValues.get_relation_key_values(
            obj, (field_object.to_field_instance.model_field_name,), f"Filter '{key}'"
        )
        if key_value is None:
            raise QueryError(f"'{key}' got an unsaved {related_model.__name__} instance")
        return key_value

    @staticmethod
    def get_forward_relation_lookup_value(key: str, field_object: RelationalField[Model], value: Any) -> Any:
        """Converts related-model instances in a relation lookup's value into their key values.

        Args:
            key: The filter kwarg name.
            field_object: The forward FK/O2O field.
            value: The lookup value - an instance, a key value, a list of either, or a queryset.

        Returns:
            The value with every instance replaced by its ``to_field`` value, and a queryset of the
            related model narrowed to that one column.

        Raises:
            QueryError: An instance isn't of the related model, or is unsaved.
        """
        from hare.models import Model
        from hare.query.queryset import QuerySet
        from hare.query.queryset.combination.queryset_combination import QuerySetCombination
        from hare.query.statements.select.combined_query import CombinedQuery

        to_field_name = field_object.to_field_instance.model_field_name
        related_model = field_object.related_model

        def get_key_value(element: Any) -> Any:
            if not isinstance(element, Model):
                return element
            return RelationFilters.get_related_instance_key_value(key, field_object, element)

        if (
            isinstance(value, QuerySet)
            and not QuerySetCombination.selects_values(value)
            and value.model is related_model
        ):
            if value._combination is not None:
                return cast("CombinedQuery", value._get_compiler())._get_field_values_query(to_field_name)
            value._build_conditions_for_copies()
            field_values_query = value._get_field_values_query(to_field_name)
            # Made again for each build - its values come from the queryset.
            field_values_query._plan_origin = value
            return field_values_query
        if isinstance(value, (list, tuple, set)):
            return [get_key_value(element) for element in value]
        return get_key_value(value)

    @staticmethod
    def get_forward_relation_isnull_kwarg(
        expression_context: ExpressionContext, key: str, value: Any
    ) -> QueryModifier | None:
        """Resolves ``<fk>__isnull``/``<fk>__not_isnull``/``<fk>=None`` on a forward relation whose
        target has a default scope: a hidden target counts as no target. The key column
        (``<fk>_id``) keeps comparing the stored value.

        Args:
            expression_context: The context the filter is resolved in.
            key: The filter key.
            value: The filter value.

        Returns:
            The modifier, None if ``key`` isn't such a lookup or its target has no default scope
            left.
        """
        from hare.query.scopes.row_scopes import RowScopes

        model = expression_context.model
        relation_name, _, lookup = key.partition("__")
        if lookup == Lookup.EXACT:
            if value is not None:
                return None
            is_empty = True
        elif lookup in {Lookup.ISNULL, Lookup.NOT_ISNULL} and isinstance(value, bool):
            is_empty = value if lookup == Lookup.ISNULL else not value
        else:
            return None
        if relation_name not in model._meta.foreign_key_fields and relation_name not in model._meta.one_to_one_fields:
            return None
        relation_field = cast("RelationalField[Model]", model._meta.fields_map[relation_name])
        related_model = relation_field.related_model
        related_meta = related_model._meta
        table = expression_context.table
        related_table = related_meta.basetable.as_(
            Identifiers.get_within_limit(f"{table.get_table_name()}__{relation_name}__live")
        )
        visible_target_criterion = RowScopes.of(related_model).get_criterion(
            related_table,
            visibility=expression_context.visibility,
            dialect=expression_context.dialect,
            connection=expression_context.connection,
        )
        if visible_target_criterion is None:
            return None
        # One column per key component - a composite key is null exactly when every column is.
        source_columns = [
            table[model._meta.fields_db_projection[source_field]] for source_field in relation_field.source_fields
        ]
        target_columns = [
            related_table[related_meta.fields_db_projection[to_field_instance.model_field_name]]
            for to_field_instance in relation_field.to_field_instances
        ]
        key_matches: Criterion = visible_target_criterion
        for target_column, source_column in zip(target_columns, source_columns, strict=True):
            key_matches = (target_column == source_column) & key_matches
        visible_target_exists = ExistsTerm(QueryBuilder().from_(related_table).select(Star()).where(key_matches))
        if not is_empty:
            return QueryModifier(where_criterion=visible_target_exists)
        key_is_null: Criterion = source_columns[0].isnull()
        for source_column in source_columns[1:]:
            key_is_null &= source_column.isnull()
        return QueryModifier(where_criterion=key_is_null | Not(visible_target_exists))

    @staticmethod
    def get_relation_filter_parameters(
        expression_context: ExpressionContext, lookup_info: LookupInfo, value: Any
    ) -> tuple[LookupInfo, Any]:
        """The lookup a filter kwarg resolves to and the value it compares - a lookup on a forward
        FK/O2O itself compares its key column (``author__in`` is ``author_id__in``) with the
        related instances' keys, one on a to-many relation the related instances' primary keys.

        Args:
            expression_context: The context the filter is resolved in.
            lookup_info: The filter key's description.
            value: The filter value.

        Returns:
            The lookup's description and the value.
        """

        key = lookup_info.key
        compared_lookup_info = lookup_info
        filter_value: Any = value
        meta = expression_context.model._meta
        if lookup_info.target == LookupTarget.RELATION and len(lookup_info.relations) == 1:
            relation = cast("RelationalField[Model]", lookup_info.relations[0])
            if (
                relation.model_field_name in meta.foreign_key_fields
                or relation.model_field_name in meta.one_to_one_fields
            ):
                from hare.models import Model

                source_field_name = cast("str", relation.source_field)
                compared_lookup_info = meta._get_lookup_info(
                    f"{source_field_name}__{lookup_info.lookup}" if lookup_info.lookup else source_field_name
                )
                if lookup_info.lookup:
                    filter_value = RelationFilters.get_forward_relation_lookup_value(key, relation, value)
                elif isinstance(value, Model):
                    filter_value = RelationFilters.get_related_instance_key_value(key, relation, value)
            else:
                # A to-many relation compares the related rows' primary key - an instance names its own.
                filter_value = getattr(value, "pk", value)
        return compared_lookup_info, filter_value

    @staticmethod
    def get_filter_join_table(model: type[Model], key: str, table: Table, filter_table: Table) -> Table:
        """Aliases the joined table of a backward FK/O2O lookup, so a self-referential relation
        (or a chain whose base table is the related table) can tell the two occurrences apart.

        Args:
            model: The model the lookup key is resolved against.
            key: The lookup key, e.g. ``team_members__isnull``.
            table: The table `model` is currently read through.
            filter_table: The unaliased related table registered for the lookup.

        Returns:
            The aliased table for a backward relation, `filter_table` unchanged otherwise.
        """
        relation_name = key.partition("__")[0]
        if not isinstance(model._meta.fields_map.get(relation_name), BackwardForeignKeyRelation):
            return filter_table
        return filter_table.as_(Identifiers.get_within_limit(f"{table.get_table_name()}__{relation_name}__direct"))
