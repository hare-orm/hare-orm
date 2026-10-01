from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import (
    FieldError,
    QueryError,
)
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.base.arithmetic_expression_mixin import ArithmeticExpressionMixin
from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.base.expression_result import ExpressionResult
from hare.query.expressions.enums import ValueRefOrigin
from hare.query.expressions.f import F
from hare.query.lookup_paths import LookupPaths
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql.terms.base.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from collections.abc import Iterator

from hare.query.expressions.outer_aggregate_term import OuterAggregateTerm
from hare.query.expressions.outer_query_state import (
    outer_aggregate_references,
    outer_expression_context,
    outer_extra_joins,
    outer_reference_terms,
)
from hare.query.expressions.qualified_outer_field import QualifiedOuterField


class OuterRef(ArithmeticExpressionMixin):
    """A reference to a field of the outer query from a child queryset in
    ``Exists(...)``/``Subquery(...)``; outside one it raises ``QueryError``. The field may be
    reached through a relation (``OuterRef("author__name")``) - the join is added to the outer
    query.

    Example:
        ``Ticket.objects.annotate(open=Exists(Report.objects.filter(ticket_id=OuterRef("id"),
        status="open")))``
    """

    def __init__(self, field: str) -> None:
        if not isinstance(field, str):
            raise QueryError(
                f"OuterRef(...) expects a field NAME (str), got {field!r} - nesting one OuterRef "
                "inside another isn't supported, since there's only ever one enclosing outer "
                "query to refer to."
            )
        self.field = field

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """The enclosing query's column the reference reads - it binds no value.

        Args:
            context: The context of the query the reference is in.

        Returns:
            The description, None for a path across a relation: the enclosing query joins the
            relation, a default scope's condition among the join's.
        """
        if "__" in self.field:
            return None
        return PlanDescription((OuterRef, self.field), [])

    @staticmethod
    def _record_outer_reference_terms(column_terms: list[Term]) -> None:
        """Hands the outer columns this reference reads to the enclosing Exists/Subquery.

        Args:
            column_terms: The outer query's column terms.
        """
        if (reference_terms := outer_reference_terms.get()) is not None:
            reference_terms.extend(column_terms)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        outer_context = outer_expression_context.get()
        if outer_context is None:
            raise QueryError("OuterRef() can only be used inside a queryset wrapped in Exists(...)")
        if "__" in self.field:
            term, joins, nested_output_field = LookupPaths.get_nested_field(
                outer_context.model,
                outer_context.table,
                self.field,
                visibility=outer_context.visibility,
                select_related_extra_conditions=outer_context.select_related_extra_conditions,
                dialect=expression_context.dialect,
                connection=expression_context.connection,
            )
            # Every join table of a relation path is aliased, so the term is always qualified.
            extra_joins = outer_extra_joins.get()
            if extra_joins is not None:
                extra_joins.extend(joins)
            # A to-many hop joined into the outer query repeats its rows like any other JOIN.
            if (tracker := AggregatedMultiValuedPaths.get_from(outer_context)) is not None:
                tracker.record_lookup(outer_context.model, self.field, outer_context.select_related_path_prefix)
            self._record_outer_reference_terms(term.get_group_by_column_terms())
            return ExpressionResult(term=term, output_field=nested_output_field)
        # A term that always qualifies its column: the reference crosses a query boundary, and the
        # child query computes its own namespacing - an unqualified "id" would bind to the child's
        # table.
        outer_meta = outer_context.model._meta
        field_name = outer_meta.pk_attr if self.field == "pk" and isinstance(outer_meta.pk_attr, str) else self.field
        field_object = outer_meta.fields_map.get(field_name)
        if field_name not in outer_meta.fields_db_projection and isinstance(
            field_object, (ForeignKeyFieldInstance, OneToOneFieldInstance)
        ):
            # A forward relation's own name means its key column, as F("author") does.
            if len(field_object.source_fields) > 1:
                raise FieldError(
                    f"OuterRef('{self.field}'): {outer_context.model.__name__}.{self.field} is a "
                    "composite relation - reference each of its key columns separately instead."
                )
            field_name = cast("str", field_object.source_field)
            field_object = outer_meta.fields_map.get(field_name)
        column = outer_meta.fields_db_projection.get(field_name)
        if column is not None:
            outer_field_term = QualifiedOuterField(outer_context.table, column)
            self._record_outer_reference_terms([outer_field_term])
            return ExpressionResult(term=outer_field_term, output_field=field_object)
        if field_name in outer_context.annotations:
            if expression_context.value_wrapper_refs is not None:
                # The enclosing query's annotation is resolved again here, its values out of the
                # order a plan lists them in - the query keeps no plan.
                expression_context.value_wrapper_refs.append((ValueRefOrigin.SUBQUERY, None))
            annotation_result = F(field_name).get_result(outer_context)
            annotation_term = annotation_result.term
            annotation_nodes: Iterator[Any] = annotation_term.nodes_()
            if any(getattr(node, "is_analytic", False) is True for node in annotation_nodes):
                raise QueryError(
                    f"OuterRef('{self.field}') references a window function (Window(...)) annotation - "
                    "SQL does not allow a window function inside a subquery of the query computing it. "
                    "Wrap the outer queryset in a subquery and reference that instead."
                )
            if annotation_term.contains_aggregate:
                if (aggregate_references := outer_aggregate_references.get()) is not None:
                    aggregate_references.append(field_name)
                annotation_term = OuterAggregateTerm(annotation_term)
            outer_table = outer_context.table
            if not outer_table.alias:
                # Aliased under its own name so every outer column inside the annotation renders
                # table-qualified, whatever namespace setting the child query renders with.
                qualified_outer_table = copy(outer_table)
                qualified_outer_table.alias = outer_table.get_table_name()
                annotation_term = annotation_term.replace_table(outer_table, qualified_outer_table)
            self._record_outer_reference_terms(annotation_term.get_group_by_column_terms())
            return ExpressionResult(term=annotation_term, output_field=annotation_result.output_field)  # type:ignore[call-overload]
        raise FieldError(f"OuterRef('{self.field}'): no such field on {outer_context.model.__name__}")
