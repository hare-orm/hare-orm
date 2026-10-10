from __future__ import annotations

from hare.fields.data.numeric.big_int_field import BigIntField
from hare.query.expressions import Aggregate
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.queryset.concrete_field_paths import ConcreteFieldPaths
from hare.sql import functions
from hare.sql.terms.case.case import Case
from hare.sql.terms.values.value_wrapper import ValueWrapper


class Count(Aggregate):
    """Counts the number of entries for that column, e.g. ``Count("field_name")``."""

    database_function = functions.Count

    #: Shared, long-lived instance - the statement plans hold an annotation's output
    #: field weakly, so a fresh instance per call would be collected immediately.
    COUNT_OUTPUT_FIELD = BigIntField()

    value_field = COUNT_OUTPUT_FIELD

    def _accepts_encrypted_argument(self) -> bool:
        # Counting non-NULL values never reads them; counting DISTINCT values would compare
        # ciphertexts, which differ for equal values.
        return not self.distinct

    @staticmethod
    def _is_never_null_column(expression_context: ExpressionContext, field_name: str) -> bool:
        """Whether a name is a column of the queried model's own table that holds no NULL.

        Args:
            expression_context: The context the count is resolved in.
            field_name: The counted name.

        Returns:
            True for such a column.
        """
        meta = expression_context.model._meta
        return field_name in meta.fields_db_projection and not meta.fields_map[field_name].null

    def _get_nested_field(self, expression_context: ExpressionContext, field: str) -> ExpressionResult:
        """Resolves a counted name - a composite key (``pk``, or a relation to one) counts its
        rows by a key column, NULL exactly when there's no row, and distinct keys by the whole key.

        Args:
            expression_context: The context the count is resolved in.
            field: The counted name.

        Returns:
            The counted term.
        """

        if field in expression_context.annotations:
            return super()._get_nested_field(expression_context, field)
        component_paths = ConcreteFieldPaths.get_paths(expression_context.model, field)
        if len(component_paths) <= 1:
            result = super()._get_nested_field(expression_context, field)
            if (
                not self.distinct
                and component_paths
                and not expression_context.select_related_path_prefix
                and self._is_never_null_column(expression_context, component_paths[0])
            ):
                result.term = expression_context.dialect.renderers.get_never_null_column_count_argument(result.term)
            return result
        component_results = [
            super(Count, self)._get_nested_field(expression_context, path) for path in component_paths
        ]
        joins = component_results[0].joins
        if not self.distinct:
            return ExpressionResult(term=component_results[0].term, joins=joins)
        component_terms = [component_result.term for component_result in component_results]
        key_term = expression_context.dialect.renderers.get_composite_distinct_key(component_terms)
        # A missing key (a null foreign key, no related row) stays NULL, so it isn't counted.
        counted_term = Case().when(component_terms[0].isnull(), ValueWrapper(None)).else_(key_term)
        return ExpressionResult(term=counted_term, joins=joins)
