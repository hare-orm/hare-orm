from __future__ import annotations

from hare.dialects.postgresql.fields.search import TSVectorField
from hare.dialects.postgresql.search.criterion.declarations import TsInfixOperator
from hare.dialects.postgresql.search.criterion.search_arguments import SearchArguments
from hare.dialects.postgresql.search.enums import TsWeight
from hare.dialects.postgresql.search.types import ConfigInput, VectorInput, WeightInput
from hare.dialects.postgresql.search.vector.search_vector_combinable import SearchVectorCombinable
from hare.exceptions import QueryError
from hare.fields.encrypted.encrypted_field_mixin import EncryptedFieldMixin
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.expressions.base.expression_result import TableCriterionTuple
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql.functions.cast import Cast
from hare.sql.functions.declarations import Coalesce
from hare.sql.terms.base.literal_value import LiteralValue
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.functions.function import Function as HareSqlFunction


class SearchVector(SearchVectorCombinable, Expression):
    """Builds a ``TO_TSVECTOR(...)`` expression from one or more fields/expressions, for use in
    an annotation or a `SearchCriterion`.

    Args:
        expressions: Field names or expressions to concatenate into the vector.
        config: The text search configuration name (e.g. ``"english"``).
        weight: A weight letter (``"A"``-``"D"``) to apply via ``SETWEIGHT``.

    Raises:
        ValueError: If no expressions are given.
    """

    def __init__(
        self,
        *expressions: VectorInput,
        config: ConfigInput | None = None,
        weight: WeightInput | None = None,
    ):
        if not expressions:
            raise QueryError("SearchVector requires at least one expression.")
        self.expressions = expressions
        self.config = config
        self.weight = weight

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """Each source, the configuration and the weight - a weight letter is written into the
        SQL text, any other weight bound.

        Args:
            context: The context the vector is resolved in.

        Returns:
            The description, None for an argument given as a SQL term.
        """
        arguments = (
            *((expression, True) for expression in self.expressions),
            *(((self.config, False),) if self.config is not None else ()),
        )
        weight_description = (
            PlanDescription(self.weight, [])
            if self.weight is None or isinstance(self.weight, str)
            else SearchArguments.get_plan_description(self.weight, context, treat_str_as_field=False)
        )
        return PlanDescription.combine(
            (SearchVector, len(self.expressions), self.config is not None),
            (
                *(
                    SearchArguments.get_plan_description(argument, context, treat_str_as_field=treat_str_as_field)
                    for argument, treat_str_as_field in arguments
                ),
                weight_description,
            ),
        )

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        expression_results = [
            SearchArguments.get_result(expr, expression_context, treat_str_as_field=True) for expr in self.expressions
        ]
        terms: list[Term] = []
        for expression, expression_result in zip(self.expressions, expression_results, strict=True):
            source_field = (
                expression.get_value_field(expression_result)
                if isinstance(expression, Expression)
                else expression_result.output_field  # type:ignore[call-overload]
            )
            EncryptedFieldMixin.raise_if_encrypted(source_field, "SearchVector()")
            # A source that isn't a text column is cast to text; COALESCE keeps one NULL source from
            # making the whole vector NULL.
            source_term = (
                expression_result.term
                if TSVectorField.is_text_source_field(source_field)
                else Cast(expression_result.term, "TEXT")
            )
            terms.append(Coalesce(source_term, ""))
        joins = ExpressionResult.dedup_joins(*(item.joins for item in expression_results))

        combined = terms[0]
        for term in terms[1:]:
            combined = TsInfixOperator(combined, " || ", ValueWrapper(" "))
            combined = TsInfixOperator(combined, " || ", term)

        args = [combined]
        if self.config is not None:
            config_result = SearchArguments.get_result(self.config, expression_context, treat_str_as_field=False)
            args = [config_result.term, combined]
            joins = ExpressionResult.dedup_joins(joins, config_result.joins)

        vector_term = HareSqlFunction("TO_TSVECTOR", *args)

        if self.weight is not None:
            if isinstance(self.weight, str):
                # setweight()'s weight is of type "char": a text parameter is rejected, a string
                # literal isn't. Validated by TsWeight.
                weight_term: Term = LiteralValue(f"'{TsWeight(self.weight.upper()).value}'")
                weight_joins: list[TableCriterionTuple] = []
            else:
                weight_result = SearchArguments.get_result(self.weight, expression_context, treat_str_as_field=False)
                # Cast through TEXT, so a bound parameter is typed as text rather than "char"
                # (which a driver can't bind from a str).
                weight_term = Cast(Cast(weight_result.term, "TEXT"), LiteralValue('"char"'))
                weight_joins = weight_result.joins
            vector_term = HareSqlFunction("SETWEIGHT", vector_term, weight_term)
            joins = ExpressionResult.dedup_joins(joins, weight_joins)

        return ExpressionResult(term=vector_term, joins=joins, output_field=TSVectorField())
