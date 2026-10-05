from __future__ import annotations

from typing import Any

from hare.dialects.postgresql.functions.aggregates.string_agg_function import StringAggFunction
from hare.exceptions import QueryError
from hare.fields.data.text.char_field import CharField
from hare.fields.data.text.text_field import TextField
from hare.fields.field import Field
from hare.query.expressions import (
    Aggregate,
    CombinedExpression,
    Expression,
    ExpressionResult,
    F,
)
from hare.sql.functions.cast import Cast
from hare.sql.terms.field import Field as HareSqlField


class StringAgg(Aggregate):
    """STRING_AGG(field::text, delimiter) - joins a column's values into one string per GROUP BY
    group. A non-text field is cast to text first, in Postgres's own text form of its type.

    Example: ``Model.objects.annotate(names=StringAgg("author__name", ", ")).group_by("id")``
    """

    database_function = StringAggFunction
    allows_order_by = True

    #: Shared, long-lived instance - the statement plans hold an annotation's output
    #: field weakly, so a fresh instance per call would be collected immediately.
    STRING_AGG_OUTPUT_FIELD = TextField()

    def __init__(self, field: str | F | CombinedExpression, delimiter: str | Expression, **options: Any) -> None:
        """
        Args:
            field: The aggregated field or expression.
            delimiter: The text between the values, or an expression of text.
            options: ``distinct``, ``_filter`` and ``order_by`` of an aggregate.

        Raises:
            QueryError: The delimiter is no text - the database joined a number by its text, or refused it.
        """
        if not isinstance(delimiter, (str, Expression)):
            raise QueryError(f"StringAgg() takes a text delimiter, got {type(delimiter).__name__}")
        super().__init__(field, delimiter, **options)

    def _get_output_field(
        self, function_arg: ExpressionResult, default_results: list[ExpressionResult]
    ) -> Field[Any] | None:
        """The joined values are text whatever the aggregated field's type.

        Args:
            function_arg: The resolved main argument.
            default_results: The resolved delimiter.

        Returns:
            The shared text field.
        """
        return self.STRING_AGG_OUTPUT_FIELD

    def _get_function_term(self, function_arg: ExpressionResult, default_terms: list[Any]) -> Any:
        """Orders the distinct values of a text field by the text they are joined as - PostgreSQL takes
        an ``ORDER BY`` of a ``DISTINCT`` aggregate only of its argument, ``CAST(value AS TEXT)``.

        Raises:
            QueryError: ``distinct=True`` orders by a non-text field it joins - its text would be ordered
                ('10' before '9'), not its values.
        """
        if self.distinct and function_arg.aggregate_orderings:
            orderings = []
            for term, order in function_arg.aggregate_orderings:
                if self.is_aggregated_term(term, function_arg.term):
                    if not isinstance(function_arg.output_field, (CharField, TextField)):
                        raise QueryError(
                            "StringAgg(distinct=True, order_by=...) orders a non-text field by its text, not its "
                            "values - order by another expression, or join without distinct"
                        )
                    term = Cast(term, "TEXT")
                orderings.append((term, order))
            function_arg.aggregate_orderings = orderings
        return super()._get_function_term(function_arg, default_terms)

    @staticmethod
    def is_aggregated_term(term: Any, aggregated_term: Any) -> bool:
        """Whether an ordering's term is the column the aggregate joins.

        Args:
            term: The ordering's term.
            aggregated_term: The aggregated term.

        Returns:
            Whether both are the same column of the same table.
        """
        return (
            isinstance(term, HareSqlField)
            and isinstance(aggregated_term, HareSqlField)
            and term.name == aggregated_term.name
            and term.table == aggregated_term.table
        )
