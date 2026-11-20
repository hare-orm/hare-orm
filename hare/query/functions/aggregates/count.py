from __future__ import annotations

from collections.abc import Sequence

from hare.fields.data.numeric.big_int_field import BigIntField
from hare.query.expressions import Aggregate
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult, TableCriterionTuple
from hare.query.key_columns import KeyColumns
from hare.query.queryset.concrete_field_paths import ConcreteFieldPaths
from hare.query.statements.projections.projected_value import ProjectedValue
from hare.sql import functions
from hare.sql.terms.case.case import Case
from hare.sql.terms.term import Term
from hare.sql.terms.tuple import Tuple
from hare.sql.terms.values.value_wrapper import ValueWrapper


class Count(Aggregate):
    """The number of the values of a group that aren't NULL - of rows for ``Count("id")`` or a column
    without NULLs; ``distinct=True`` counts each value once."""

    database_function = functions.Count

    #: Shared result descriptors avoid allocating a field for each expression build.
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
        meta = expression_context.binding
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

        annotation = expression_context.annotations.get(field)
        if isinstance(annotation, ProjectedValue) and isinstance(annotation.term, Tuple):
            return self._get_composite_argument(expression_context, annotation.term.values, [])
        if annotation is not None:
            key_path = KeyColumns.get_primary_key_name(
                expression_context.binding, expression_context.annotations, field
            )
            if key_path == field:
                return super()._get_nested_field(expression_context, field)
            field = key_path
        component_paths = ConcreteFieldPaths.get_paths(expression_context.binding, field)
        if len(component_paths) <= 1:
            result = super()._get_nested_field(expression_context, field)
            if (
                not self.distinct
                and component_paths
                and not expression_context.select_related_path_prefix
                and self._is_never_null_column(expression_context, component_paths[0])
            ):
                result.term = expression_context.dialect.renderers.get_never_null_column_count_argument(
                    result.term, is_filtered=self.filter is not None
                )
            return result
        component_results = [
            super(Count, self)._get_nested_field(expression_context, path) for path in component_paths
        ]
        return self._get_composite_argument(
            expression_context, [result.term for result in component_results], component_results[0].joins
        )

    def _get_composite_argument(
        self, expression_context: ExpressionContext, component_terms: Sequence[Term], joins: list[TableCriterionTuple]
    ) -> ExpressionResult:
        """Count key rows or distinct complete keys using the same semantics for derived columns."""
        if not self.distinct:
            return ExpressionResult(term=component_terms[0], joins=joins)
        key_term = expression_context.dialect.renderers.get_composite_distinct_key(component_terms)
        # A missing key (a null foreign key, no related row) stays NULL, so it isn't counted.
        counted_term = Case().when(component_terms[0].isnull(), ValueWrapper(None)).else_(key_term)
        return ExpressionResult(term=counted_term, joins=joins)
