from hare.fields.data.numeric.big_int_field import BigIntField
from hare.query.expressions import Aggregate
from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.base.expression_result import ExpressionResult
from hare.sql import functions
from hare.sql.terms.arithmetic.case import Case
from hare.sql.terms.base.value_wrapper import ValueWrapper


class Count(Aggregate):
    """Counts the number of entries for that column, e.g. ``Count("field_name")``."""

    database_func = functions.Count

    #: Shared, long-lived instance - the statement plans hold an annotation's output
    #: field weakly, so a fresh instance per call would be collected immediately.
    COUNT_OUTPUT_FIELD = BigIntField()

    value_field = COUNT_OUTPUT_FIELD

    def _accepts_encrypted_argument(self) -> bool:
        # Counting non-NULL values never reads them; counting DISTINCT values would compare
        # ciphertexts, which differ for equal values.
        return not self.distinct

    def _get_nested_field(self, expression_context: ExpressionContext, field: str) -> ExpressionResult:
        """Resolves a counted name - a composite key (``pk``, or a relation to one) counts its
        rows by a key column, NULL exactly when there's no row, and distinct keys by the whole key.

        Args:
            expression_context: The context the count is resolved in.
            field: The counted name.

        Returns:
            The counted term.
        """
        from hare.query.statements.awaitable_query import AwaitableQuery

        if field in expression_context.annotations:
            return super()._get_nested_field(expression_context, field)
        component_paths = AwaitableQuery.get_concrete_field_paths(expression_context.model, field)
        if len(component_paths) <= 1:
            return super()._get_nested_field(expression_context, field)
        component_results = [
            super(Count, self)._get_nested_field(expression_context, path) for path in component_paths
        ]
        joins = component_results[0].joins
        if not self.distinct:
            return ExpressionResult(term=component_results[0].term, joins=joins)
        component_terms = [component_result.term for component_result in component_results]
        key_term = expression_context.dialect.get_composite_distinct_key(component_terms)
        # A missing key (a null foreign key, no related row) stays NULL, so it isn't counted.
        counted_term = Case().when(component_terms[0].isnull(), ValueWrapper(None)).else_(key_term)
        return ExpressionResult(term=counted_term, joins=joins)
