from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.enums import ParameterPosition
from hare.exceptions import FieldError
from hare.fields.data.json.json_path_field import JSONPathField
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.enums import Lookup, LookupTarget
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.conditions.query_modifier import QueryModifier
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.joins.named_join import NamedJoin
from hare.query.expressions.raw_sql import RawSQL
from hare.query.expressions.subqueries.subquery import Subquery
from hare.query.expressions.value import Value
from hare.query.expressions.value_references.array_value_reference import ArrayValueReference
from hare.query.expressions.value_references.encoded_value_reference import EncodedValueReference
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.expressions.value_references.like_value_reference import LikeValueReference
from hare.query.expressions.value_references.list_parameter_value_reference import ListParameterValueReference
from hare.query.expressions.value_references.list_value_reference import ListValueReference
from hare.query.expressions.value_references.literal_value_reference import LiteralValueReference
from hare.query.expressions.value_references.open_range_value_reference import OpenRangeValueReference
from hare.query.expressions.value_references.range_value_reference import RangeValueReference
from hare.query.expressions.value_references.rebuilt_criterion_value_reference import RebuiltCriterionValueReference
from hare.query.expressions.value_references.related_key_value_reference import RelatedKeyValueReference
from hare.query.expressions.value_references.related_value_reference import RelatedValueReference
from hare.query.expressions.value_references.row_list_value_reference import RowListValueReference
from hare.query.expressions.value_references.scalar_value_reference import ScalarValueReference
from hare.query.filters import FieldLookups
from hare.query.filters.resolution.annotation_filters import AnnotationFilters
from hare.query.filters.resolution.composite_key_filters import CompositeKeyFilters
from hare.query.filters.resolution.filter_values import FilterValues
from hare.query.filters.resolution.json_path_filters import JsonPathFilters
from hare.query.filters.resolution.lookup_support import LookupSupport
from hare.query.filters.resolution.relation_filters import RelationFilters
from hare.query.filters.resolution.to_many_filters import ToManyFilters
from hare.query.plans.description.filter_plan_descriptions import FilterPlanDescriptions
from hare.query.plans.description.plannable import Plannable
from hare.query.plans.plan_origins import PlanOrigins
from hare.query.scopes.scope_join_conditions import ScopeJoinConditions
from hare.sql import Table
from hare.sql.functions.datetime.date_as_timestamp import DateAsTimestamp
from hare.sql.functions.datetime.timestamp_comparand import TimestampComparand
from hare.sql.terms.array import Array
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.criteria.json_attribute_criterion import JSONAttributeCriterion
from hare.sql.terms.parameters.list_parameter import ListParameter
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.expressions.conditions.q import Q
    from hare.query.expressions.value_references.composite_key_value_reference import CompositeKeyValueReference
    from hare.query.expressions.value_references.value_reference_types import ValueReference
    from hare.query.filters.lookups.field_lookup import FieldLookup
    from hare.query.lookup_info.lookup_info import LookupInfo


class FilterKwargs:
    """How the keyword filters of a condition become criteria: each key described, routed to a plain,
    custom, expanded or relation filter, and resolved against the context's model."""

    @staticmethod
    def get_kwargs(condition: Q, expression_context: ExpressionContext) -> QueryModifier:
        expression_context = condition._get_own_expression_context(expression_context)
        modifier = QueryModifier()
        recorded_paths_mark = condition._get_recorded_paths_mark(expression_context)
        own_expression_context = expression_context
        for filter_key, filter_value in condition.filters.items():
            relation_alias = own_expression_context.annotations.get(filter_key.partition("__")[0])
            if isinstance(relation_alias, NamedJoin):
                modifier = condition._combine_modifier(
                    modifier,
                    relation_alias.get_path_filter(
                        own_expression_context,
                        filter_key.partition("__")[2],
                        filter_value,
                        condition._filter_call_generation,
                        None
                        if own_expression_context.value_wrapper_references is None
                        else PlanOrigins.get_value_origin(condition, filter_key),
                    ),
                )
                continue
            LookupSupport.raise_if_lookup_unsupported(own_expression_context, filter_key)
            # A constant filter's value is part of the plan key - nothing is recorded for it.
            recording = (
                None
                if FilterPlanDescriptions.is_constant_filter(filter_key, filter_value)
                else own_expression_context.value_wrapper_references
            )
            raw_key, raw_value = JsonPathFilters.get_json_path_mirrored_kwarg(
                own_expression_context, filter_key, filter_value
            )
            expression_context, annotation_name = JsonPathFilters.get_json_path_expression_context(
                own_expression_context, raw_key
            )
            if annotation_name is not None:
                value, value_joins, value_field = FilterValues.get_filter_value(expression_context, raw_key, raw_value)
                filter_modifier, custom_reference = FilterKwargs.get_custom_kwarg(
                    expression_context,
                    annotation_name,
                    raw_key,
                    value,
                    compared_value_field=value_field,
                    records_value=recording is not None and value is raw_value,
                )
                if recording is not None and not isinstance(raw_value, Expression):
                    # An expression value recorded its own values while it was resolved
                    # (FilterValues.get_filter_value()).
                    recording.append((PlanOrigins.get_value_origin(condition, filter_key), custom_reference))
            else:
                lookup_info = FilterKwargs.get_key_lookup_info(expression_context, raw_key)
                expanded = FilterKwargs.get_expanded_kwarg(condition, expression_context, lookup_info, raw_value)
                if expanded is not None:
                    expanded_modifier, expanded_reference, value_recorded_itself = expanded
                    modifier = condition._combine_modifier(modifier, expanded_modifier)
                    if recording is not None and not value_recorded_itself:
                        recording.append((PlanOrigins.get_value_origin(condition, filter_key), expanded_reference))
                    continue
                compared_lookup_info, value = RelationFilters.get_relation_filter_parameters(
                    expression_context, lookup_info, raw_value
                )
                value, value_joins, __ = FilterValues.get_filter_value(expression_context, raw_key, value)
                # A lookup on a relation itself compares the key column or the related keys, and a
                # query or expression value is resolved - a later value converts differently, so
                # only the value given as it is binds in a plan. A forward relation compared through
                # its key column binds a plain key value as its column does: the plan key holds the
                # value's type, so a later value is a key value too - not so a list, which may hold
                # instances whatever its length.
                # An instance, or a list of instances or keys, compared through a forward relation's
                # key column binds as the keys it stands for.
                binds_related_keys = compared_lookup_info is not lookup_info and FilterKwargs.is_related_key_value(
                    lookup_info, raw_value
                )
                bindable = binds_related_keys or (
                    value is raw_value
                    and (compared_lookup_info is lookup_info or not isinstance(value, (list, tuple, set)))
                )
                filter_modifier, reference = FilterKwargs.get_regular_kwarg(
                    condition,
                    expression_context,
                    compared_lookup_info,
                    value,
                    expression_context.table,
                    bindable=bindable,
                )
                recorded_reference: ValueReference | None = reference
                if binds_related_keys and reference is not None:
                    relation = cast("RelationalField[Model]", lookup_info.relations[0])
                    recorded_reference = RelatedKeyValueReference(
                        reference, relation.related_model, True, relation.to_field_instance.model_field_name
                    )
                if recording is None or isinstance(value, Subquery) or isinstance(raw_value, Expression):
                    # A subquery or an expression value recorded its own values while it was
                    # resolved (FilterValues.get_filter_value(), Subquery.get_result()).
                    pass
                elif FilterPlanDescriptions.is_constant_list_condition(raw_key, raw_value):
                    # An empty __in/__not_in list, or one of none but None - a constant condition
                    # with no value to bind.
                    pass
                elif FilterPlanDescriptions.is_union_query(raw_value):
                    # A union recorded its own values while it was built into the filter
                    # (FilterValues.get_subquery_filter_value()).
                    pass
                elif isinstance(raw_value, RawSQL) and bindable:
                    # The SQL text is part of the plan key; its parameters are the values.
                    ExpressionArguments.record_parameters(recording, raw_value, "parameters", raw_value.parameters)
                else:
                    recording.append((PlanOrigins.get_value_origin(condition, filter_key), recorded_reference))
            if value_joins:
                # An expression value crossing a relation of its own (F("relation__field")) - its
                # joins come with this kwarg's modifier, as its term does.
                filter_modifier = QueryModifier(joins=value_joins) & filter_modifier

            modifier = condition._combine_modifier(modifier, filter_modifier)
        return condition._finalize_modifier(modifier, own_expression_context, recorded_paths_mark)

    @staticmethod
    def process_filter_kwarg(
        expression_context: ExpressionContext,
        lookup_info: LookupInfo,
        value: Any,
        table: Table,
        join_table: Table | None = None,
    ) -> tuple[
        Criterion,
        tuple[Table, Criterion] | None,
        Field[Any] | None,
        Field[Any] | None,
        Callable[..., Any] | None,
        Callable[..., Any],
        Any,
        Term | None,
    ]:
        """Builds the criterion of a lookup on a field's value, or on a many-to-many or backward
        relation itself (joined to its through or related table).

        Args:
            expression_context: The context the filter is resolved in.
            lookup_info: The filter key's description.
            value: The filter value.
            table: The table the filtered model is read through.
            join_table: The table a relation's lookup compares on, when it is joined already.

        Returns:
            The criterion; the join a relation's lookup adds; the field compared (None for a
            relation, or for a value that is a SQL term); the field the value was converted by;
            the lookup's value encoder (None for the field's own conversion); the operator; the
            converted value the criterion embeds; and the term it compares (None for a relation).
        """
        if lookup_info.target == LookupTarget.RELATION:
            criterion, join, operator, value = RelationFilters.get_relation_lookup_criterion(
                expression_context, lookup_info, value, table, join_table
            )
            return criterion, join, None, None, None, operator, value, None
        model = expression_context.model
        field_lookup = cast("FieldLookup", lookup_info.field_lookup)
        model_field = cast("Field[Any]", lookup_info.field)
        value_field = cast("Field[Any]", lookup_info.value_field)  # type: ignore[call-overload]
        # Like Django, `field=None` and `field__iexact=None` mean `field__isnull=True`; `field__not=None`
        # means `field__isnull=False`.
        if value is None and (lookup_info.term_transforms or not lookup_info.transforms):
            if (
                lookup_info.lookup in {Lookup.EXACT, Lookup.IEXACT}
                and (isnull_lookup := FieldLookups.get(value_field).get(Lookup.ISNULL)) is not None
            ):
                field_lookup = isnull_lookup
                value = True
            elif (
                lookup_info.lookup == Lookup.NOT
                and (not_isnull_lookup := FieldLookups.get(value_field).get(Lookup.NOT_ISNULL)) is not None
            ):
                field_lookup = not_isnull_lookup
                value = True
        field_object: Field[Any] | None = None
        value_encoder: Callable[..., Any] | None = None
        encoder_field = GeneratedField.get_effective_field(value_field)
        if not isinstance(value, Term):
            field_object = model_field
            value_encoder = field_lookup.value_encoder
            if value_encoder is not None:
                # A GeneratedField is encoded as its output_field - an encoder reaches for that
                # field's own attributes (a range's element type, an array's base_field).
                value = value_encoder(value, model, encoder_field, expression_context.dialect)
            else:
                value = expression_context.dialect.types.get_lookup_value(value_field, value, model)
        operator = expression_context.dialect.filter_operators.get_operator(field_lookup)
        term: Term = table[
            model_field.source_field
            or model._meta.fields_db_projection.get(model_field.model_field_name)
            or model_field.model_field_name
        ]
        if field_object is not None:
            function_cast = field_object.get_function_cast(expression_context.dialect)
            if function_cast is not None:
                term = function_cast(field_object, term)
        for term_transform in lookup_info.term_transforms:
            term = term_transform(term)
        if isinstance(value, TimestampComparand):
            term = DateAsTimestamp(term, value.zone_name)
        criterion = operator(term, value)
        if model_field.sensitive:
            FilterKwargs.hide_sensitive_values(criterion)
        # `value` is the converted value the criterion embeds - a plan binds a later value only
        # where the criterion holds the encoder's own output (identity, not equality), see
        # FilterKwargs.get_regular_kwarg().
        return criterion, None, field_object, encoder_field, value_encoder, operator, value, term

    @staticmethod
    def is_related_key_value(lookup_info: LookupInfo, value: Any) -> bool:
        """Whether a value of a lookup on a forward relation stands for related keys - an instance
        of the related model, or a list of instances or plain keys.

        Args:
            lookup_info: The lookup on the relation.
            value: The value.

        Returns:
            True for such a value.
        """
        related_model = cast("RelationalField[Model]", lookup_info.relations[0]).related_model
        if isinstance(value, related_model):
            return True
        return isinstance(value, (list, tuple, set)) and not any(
            isinstance(element, (Term, Expression, Plannable)) for element in value
        )

    @staticmethod
    def hide_sensitive_values(criterion: Criterion) -> None:
        """Marks every value a criterion on a ``sensitive=True`` field binds - each comes from the
        field's value - so no log, event or error shows its parameter (``QueryParameters``).

        Args:
            criterion: The criterion.
        """
        node: Any
        for node in criterion.nodes_():
            if isinstance(node, (ValueWrapper, Array)):
                node.sensitive = True
            elif isinstance(node, ListParameter):
                node.get_parameter_source().sensitive = True

    @staticmethod
    def get_regular_kwarg(
        condition: Q,
        expression_context: ExpressionContext,
        lookup_info: LookupInfo,
        value: Any,
        table: Table,
        *,
        bindable: bool,
    ) -> tuple[
        QueryModifier,
        ScalarValueReference
        | ListValueReference
        | ListParameterValueReference
        | RangeValueReference
        | OpenRangeValueReference
        | RebuiltCriterionValueReference
        | RelatedValueReference
        | LikeValueReference
        | ArrayValueReference
        | EncodedValueReference
        | None,
    ]:
        key = lookup_info.key
        many_to_many_isnull_modifier = ToManyFilters.get_many_to_many_isnull_kwarg(
            expression_context, key, value, table
        )
        if many_to_many_isnull_modifier is not None:
            return many_to_many_isnull_modifier, None
        if FilterKwargs.crosses_relation(lookup_info):
            modifier, nested_reference = RelationFilters.get_nested_filter(
                condition, expression_context, key, value, table
            )
            return modifier, nested_reference if bindable else None

        criterion, join, field_object, cache_reference_field, value_encoder, operator, encoded_value, term = (
            FilterKwargs.process_filter_kwarg(expression_context, lookup_info, value, table)
        )
        joins = [join] if join else []
        if joins:
            # A lookup on a relation itself (.filter(tags=obj), tags__in=..., ...) joins its
            # through or related table here, scoped like any other JOIN to that model.
            relation = cast("RelationalField[Model]", lookup_info.relations[-1])
            relation_name = relation.model_field_name
            tracker = AggregatedMultiValuedPaths.get_from(expression_context)
            if relation.is_multi_valued and tracker is not None:
                prefix = expression_context.select_related_path_prefix
                tracker.record_path(f"{prefix}__{relation_name}" if prefix else relation_name)
            if isinstance(relation, BackwardForeignKeyRelation):
                # The join targets the related model's own table, so its soft-delete/tenant scope
                # belongs in the ON clause, as for a nested books__field=... lookup.
                ScopeJoinConditions.fold_ambient_scope_into_join(
                    joins,
                    relation.related_model,
                    visibility=expression_context.visibility,
                    dialect=expression_context.dialect,
                    connection=expression_context.connection,
                )
                join = joins[0]
            else:
                ScopeJoinConditions.fold_through_model_ambient_scope_into_join(
                    joins,
                    relation,
                    visibility=expression_context.visibility,
                    dialect=expression_context.dialect,
                    connection=expression_context.connection,
                )
                join = joins[0]
                if isinstance(relation, ManyToManyFieldInstance):
                    visible_target_criterion = ToManyFilters.join_visible_many_to_many_target(
                        expression_context, relation, relation_name, joins, table
                    )
                    if visible_target_criterion is not None:
                        criterion &= visible_target_criterion
        # field_object is None for a relation's lookup (its JOIN routes a negation through
        # ToManyFilters.negate_across_joins()) and for a value that is a SQL term (F()/Subquery/OuterReference/... -
        # treated as possibly NULL).
        if field_object is None and criterion.contains_aggregate:
            # A plain field compared with an aggregate (budget__lt=F("aggregate_annotation")) is a
            # HAVING condition, exactly like the mirrored aggregate_annotation__gt=F("budget").
            return QueryModifier(having_criterion=criterion, joins=joins, has_nullable_column=True), None
        modifier = QueryModifier(
            where_criterion=criterion, joins=joins, has_nullable_column=field_object is None or field_object.null
        )
        if not bindable or join is not None or field_object is None or cache_reference_field is None:
            return modifier, None
        value_reference = FilterValues.get_value_reference(
            expression_context, criterion, operator, value, encoded_value, cache_reference_field, value_encoder
        )
        if (
            value_reference is None
            and term is not None
            and cast("FieldLookup", lookup_info.field_lookup).binds_by_rebuild
        ):
            # The criterion derives what it compares from the value - a later value builds it again.
            converting_field = cache_reference_field if value_encoder is not None else lookup_info.value_field  # type: ignore[call-overload]
            return modifier, RebuiltCriterionValueReference(
                criterion, operator, term, cast("Field[Any]", converting_field), value_encoder
            )
        return modifier, value_reference

    @staticmethod
    def get_custom_kwarg(
        expression_context: ExpressionContext,
        annotation_name: str,
        key: str,
        value: Any,
        compared_value_field: Field[Any] | None = None,
        records_value: bool = False,
    ) -> tuple[QueryModifier, LiteralValueReference | ListValueReference | ListParameterValueReference | None]:
        """Resolves a filter on an annotation (``.annotate(n=...).filter(n__gte=...)``) - with the
        lookups of the annotation's output field where it is known, the lookups of a value with no
        field otherwise.

        Args:
            expression_context: The context the filter resolves in.
            annotation_name: The annotation the key starts with.
            key: The filter key.
            value: The filter value.
            compared_value_field: The field an expression value reads, when there is one.
            records_value: Whether the value is the one given (not rewritten) - only then is a
                reference returned.

        Returns:
            The modifier, and the reference a later query binds its value through - None when
            the value isn't a single parameter of a plain comparison.

        Raises:
            FieldError: The key names no lookup of the annotation's value, or one its field
                doesn't support.
        """
        raw_value = value
        suffix = key.removeprefix(annotation_name).removeprefix("__")
        field_lookup = FieldLookups.get(None).get(suffix)
        annotation = expression_context.annotations[annotation_name]
        if isinstance(annotation, Term) and not isinstance(annotation, Expression):
            annotation_info = ExpressionResult(term=annotation)
        else:
            annotation_info = annotation.get_result(expression_context)

        AnnotationFilters.check_filterable(expression_context, key, annotation_info)

        annotation_output_field = AnnotationFilters.get_compared_field(annotation, annotation_info)
        if annotation_output_field is not None:
            field_lookup = FieldLookups.get(annotation_output_field).get(suffix, field_lookup)
            if field_lookup is not None and not FieldLookups.is_supported(annotation_output_field, suffix):
                raise FieldError(annotation_output_field.get_unsupported_lookup_message())
        if field_lookup is None:
            raise LookupSupport.get_unknown_key_error(expression_context, key)
        operator = expression_context.dialect.filter_operators.get_operator(field_lookup)
        parameter_converter, encoder_field = AnnotationFilters.get_parameter_converter(
            expression_context, field_lookup, annotation_output_field, value
        )
        if parameter_converter is not None:
            value = parameter_converter(value)
        # An annotation has no field to tell whether it can be NULL - treated as nullable.
        # A Decimal binds as TEXT on SQLite and an annotation has no column affinity: the annotation
        # is cast to NUMERIC, or compared as exact decimal text when every bound value is a Decimal.
        compared_term = annotation_info.term
        if isinstance(annotation, Value):
            # A bare `$1 = $2` can't be typed - the dialect types the literal where it has to.
            compared_term = Value.get_typed_term(
                compared_term, annotation.value, ParameterPosition.COMPARED_VALUE, expression_context.dialect
            )
        if field_lookup.compares_json_path_text and isinstance(compared_term, JSONAttributeCriterion):
            compared_term = compared_term.get_text_term()
        elif isinstance(annotation_output_field, JSONPathField) and isinstance(value, Term):
            value = JsonPathFilters.get_json_comparand(value, compared_value_field, expression_context.dialect)
        if FilterValues.holds_decimal_value(value):
            compared_term = expression_context.dialect.renderers.get_decimal_compared_term(
                compared_term, only_decimals=FilterValues.holds_only_decimal_values(value)
            )
        # HAVING whenever either side reads an aggregate - the annotation itself, one mixing an
        # aggregate with a column (Count(...) + F("id")), or an F() value naming an aggregate.
        criterion = operator(compared_term, value)
        reference = (
            AnnotationFilters.get_value_reference(
                expression_context,
                criterion,
                operator,
                raw_value,
                value,
                field_lookup,
                encoder_field,
                parameter_converter,
            )
            if records_value
            else None
        )
        if criterion.contains_aggregate:
            return QueryModifier(having_criterion=criterion, has_nullable_column=True), reference
        return QueryModifier(where_criterion=criterion, has_nullable_column=True), reference

    @staticmethod
    def get_expanded_kwarg(
        condition: Q, expression_context: ExpressionContext, lookup_info: LookupInfo, value: Any
    ) -> (
        tuple[
            QueryModifier, RowListValueReference | RelatedKeyValueReference | CompositeKeyValueReference | None, bool
        ]
        | None
    ):
        """Resolves a key that becomes more than one comparison - a composite primary key, a
        relation to a composite key, a forward relation whose target a default scope may hide,
        or a lookup on a to-many relation itself.

        Args:
            condition: The condition resolved.
            expression_context: The context the filter is resolved in.
            lookup_info: The key's description.
            value: The filter value.

        Returns:
            The modifier, the reference a later query binds its value through (None when it can't)
            and whether an expression value recorded its own references instead - or None when the
            key is a single comparison.
        """
        key = lookup_info.key
        if lookup_info.target == LookupTarget.PRIMARY_KEY:
            if not lookup_info.relations:
                composite_pk = CompositeKeyFilters.get_composite_pk_kwarg(expression_context, key, value)
                return None if composite_pk is None else (*composite_pk, False)
            if lookup_info.lookup not in {Lookup.NOT, Lookup.NOT_IN}:
                return None
            # `relation__pk__not=` compares the relation's key - it is `relation__not=`, which keeps a
            # row without a related one, as `.filter()` keeps it.
            relation_key = f"{key[: key.rindex('__pk__')]}__{lookup_info.lookup}"
            key, lookup_info = relation_key, FilterKwargs.get_key_lookup_info(expression_context, relation_key)
        if lookup_info.target != LookupTarget.RELATION or len(lookup_info.relations) != 1:
            return None
        relation = cast("RelationalField[Model]", lookup_info.relations[0])
        meta = expression_context.model._meta
        if relation.model_field_name in meta.foreign_key_fields or relation.model_field_name in meta.one_to_one_fields:
            modifier = RelationFilters.get_forward_relation_isnull_kwarg(expression_context, key, value)
            if modifier is not None or len(relation.source_fields) <= 1:
                return None if modifier is None else (modifier, None, False)
            # The comparison of each key column records a reference of its own - the key's value
            # binds through them all at once.
            recording = expression_context.value_wrapper_references
            recorded_count = 0 if recording is None else len(recording)
            modifier = CompositeKeyFilters.get_composite_relation_kwarg(condition, expression_context, key, value)
            if modifier is None:
                return None
            if recording is None:
                return modifier, None, False
            component_references = [reference for _component_key, reference in recording[recorded_count:]]
            del recording[recorded_count:]
            return (
                modifier,
                CompositeKeyFilters.get_relation_key_value_reference(
                    relation, lookup_info, value, component_references
                ),
                False,
            )
        return ToManyFilters.get_to_many_relation_shortcut_kwarg(condition, expression_context, key, value)

    @staticmethod
    def get_key_lookup_info(expression_context: ExpressionContext, key: str) -> LookupInfo:
        """The description of a filter key of the model the context resolves.

        Args:
            expression_context: The context the filter is resolved in.
            key: The filter key.

        Returns:
            The description.

        Raises:
            FieldError: The key names no field or relation, or a lookup its field doesn't have.
        """
        path_prefix = expression_context.select_related_path_prefix
        label = f"Unknown filter param '{path_prefix}__{key}'" if path_prefix else None
        return expression_context.model._meta._get_lookup_info(key, label)

    @staticmethod
    def crosses_relation(lookup_info: LookupInfo) -> bool:
        """Whether a key goes on through its first relation, rather than comparing the field or
        relation it names.

        Args:
            lookup_info: The key's description.

        Returns:
            True for a key resolved on the related model.
        """
        compared_relations = 1 if lookup_info.target == LookupTarget.RELATION else 0
        return len(lookup_info.relations) > compared_relations
