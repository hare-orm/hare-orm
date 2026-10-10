from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.exceptions import FieldError, QueryError
from hare.fields.data.json.json_field import JSONField
from hare.fields.encrypted.encrypted_json_field import EncryptedJSONField
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.arithmetic.combinable_expression import CombinableExpression
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult, TableCriterionTuple
from hare.query.expressions.joins.named_join import NamedJoin
from hare.query.expressions.ordering import Ordering
from hare.query.filters.lookups.json.json_filter_parser import JsonFilterParser
from hare.query.lookup_info.lookup_paths import LookupPaths
from hare.query.lookup_info.value_paths import ValuePaths
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field


class F(CombinableExpression):
    """A reference to a model field (``F("id")``), a field of a related model
    (``F("related__field")``), an annotation, or a path into a JSON field's value
    (``F("data__owner__name")``, ``F("data__0")``).

    Args:
        name: The name of the field to reference.
    """

    # A bare reference to a model column has that column's own type - its value is decoded
    # through the field's from_db_value(), exactly like the same column in .values().
    populate_field_object = True
    plan_parts: ClassVar[DeclaredPlanParts] = (("name", PlanPartType.KEY),)

    def __init__(self, name: str) -> None:
        self.name = name

    def __eq__(self, other: object) -> bool:
        return type(other) is type(self) and cast("F", other).name == self.name

    def __hash__(self) -> int:
        return hash((type(self), self.name))

    def __repr__(self) -> str:
        return f"F({self.name!r})"

    def get_value_field(self, result: ExpressionResult) -> Field[Any] | None:
        """The field whose type the referenced value has - a relation's own name reads the
        related model's primary key.

        Args:
            result: This reference's own resolve result.

        Returns:
            The referenced column's field, or None when the reference has no model field.
        """
        output_field = result.output_field  # type:ignore[call-overload]
        if isinstance(output_field, RelationalField):
            return output_field.related_model._meta.pk
        return output_field

    def is_json_path_of(self, annotation: Any) -> bool:
        """Whether this path into a JSON annotation isn't an annotation of its own name - none, or
        the path itself registered by ``values()``/``order_by()``."""
        return annotation is None or (isinstance(annotation, F) and annotation.name == self.name)

    def asc(self, *, nulls_first: bool = False, nulls_last: bool = False) -> Ordering:
        """
        Ascending ordering by this reference, for ``.order_by()``/``.latest()``/``.earliest()``/``Meta.ordering``.

        Args:
            nulls_first: Place NULLs before every other value on any dialect.
            nulls_last: Place NULLs after every other value on any dialect. With neither flag the
                dialect's own default applies (SQLite: NULLs first for ASC; PostgreSQL: NULLs last).

        Raises:
            QueryError: If both flags are set.
        """
        return Ordering.build(self.name, is_ascending=True, nulls_first=nulls_first, nulls_last=nulls_last)

    def desc(self, *, nulls_first: bool = False, nulls_last: bool = False) -> Ordering:
        """
        Descending ordering by this reference, for ``.order_by()``/``.latest()``/``.earliest()``/``Meta.ordering``.

        Args:
            nulls_first: Place NULLs before every other value on any dialect.
            nulls_last: Place NULLs after every other value on any dialect. With neither flag the
                dialect's own default applies (SQLite: NULLs last for DESC; PostgreSQL: NULLs first).

        Raises:
            QueryError: If both flags are set.
        """
        return Ordering.build(self.name, is_ascending=False, nulls_first=nulls_first, nulls_last=nulls_last)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        term: Term
        joins: list[TableCriterionTuple] = []
        output_field = None

        main_name_part, __, rest_name_parts = self.name.partition("__")
        relation_alias = expression_context.annotations.get(main_name_part)
        if isinstance(relation_alias, NamedJoin):
            # A path through a named JOIN - a FilteredRelation's related field, a Lateral's column.
            return relation_alias.get_path_result(rest_name_parts, expression_context)
        if main_name_part in expression_context.model._meta.fetch_fields:
            # field in the format of "related_field__field" or "related_field__another_rel_field__field"
            if (tracker := AggregatedMultiValuedPaths.get_from(expression_context)) is not None:
                tracker.record_lookup(
                    expression_context.model, self.name, expression_context.select_related_path_prefix
                )
            term, joins, output_field = LookupPaths.get_nested_field(
                expression_context.model,
                expression_context.table,
                self.name,
                visibility=expression_context.visibility,
                select_related_extra_conditions=expression_context.select_related_extra_conditions,
                dialect=expression_context.dialect,
                connection=expression_context.connection,
            )
        elif rest_name_parts and (path_split := ValuePaths.get_value_path_split(expression_context.model, self.name)):
            # A path inside a JSON, array or range field's value, e.g. F("data__a__b"), F("tags__0")
            # or F("during__startswith") - a JSON path reads the JSON value itself (jsonb on
            # PostgreSQL), not its text, so it's decoded, compared and ordered as JSON.
            __, path_field, path_segments = path_split
            column = expression_context.table[expression_context.model._meta.fields_db_projection[main_name_part]]
            term, output_field = ValuePaths.get_value_path_term(column, path_field, path_segments, self.name)
        elif (
            rest_name_parts
            and main_name_part in expression_context.annotations
            and self.is_json_path_of(expression_context.annotations.get(self.name))
        ):
            # A path into a JSON annotation, e.g. F("summary__total") over annotate(summary=JSONObject(...))
            annotation_result = F(main_name_part).get_result(expression_context)
            annotation_field = annotation_result.output_field  # type:ignore[call-overload]
            if not isinstance(annotation_field, JSONField) or isinstance(annotation_field, EncryptedJSONField):
                raise FieldError(
                    f"F({self.name!r}): the annotation {main_name_part!r} isn't a JSON value, so it has no "
                    f"{rest_name_parts!r} path"
                )
            term = JsonFilterParser.get_field_path(
                annotation_result.term, JsonFilterParser.get_key_parts(rest_name_parts), as_text=False
            )
            joins = annotation_result.joins
            output_field = annotation_field.get_path_value_field()
        elif self.name in expression_context.annotations:
            # reference to another annotation, e.g. M.annotate(f1=...).annotate(f2=F("f1")).values('field')
            if self.name in expression_context.annotation_names_in_progress:
                raise QueryError(
                    f"Circular annotation reference detected: {self.name!r} depends on itself "
                    "(directly or through another annotation)"
                )
            expression_context.annotation_names_in_progress.add(self.name)
            try:
                annotation = expression_context.annotations[self.name]
                if isinstance(annotation, Term) and not isinstance(annotation, Expression):
                    term = annotation
                else:
                    referenced_result = annotation.get_result(expression_context)
                    term = referenced_result.term
                    # The referenced annotation's own value type - e.g. Length("name") is an
                    # integer, not its argument's CharField.
                    output_field = annotation.get_value_field(referenced_result)
            finally:
                expression_context.annotation_names_in_progress.discard(self.name)
        else:
            # a regular model field, e.g. F("id")
            try:
                meta = expression_context.model._meta
                field_name = (
                    meta.primary_key_attribute
                    if self.name == "pk" and isinstance(meta.primary_key_attribute, str)
                    else self.name
                )
                # Table-qualified, not a bare HareSqlField(column) - unqualified is ambiguous SQL
                # the moment any OTHER join in the same query happens to have a same-named
                # column, even though this F() always meant this specific model's own field.
                term = expression_context.table[meta.fields_db_projection[field_name]]

                if (output_field := meta.fields_map.get(field_name, None)) and (
                    function_cast := output_field.get_function_cast(expression_context.dialect)
                ):
                    term = function_cast(output_field, term)
            except KeyError:
                raise FieldError(
                    f"There is no non-virtual field {self.name} on Model {expression_context.model.__name__}"
                ) from None
        return ExpressionResult(term=term, output_field=output_field, joins=joins)
