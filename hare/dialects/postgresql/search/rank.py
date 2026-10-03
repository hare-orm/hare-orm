from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.postgresql.fields.search import TSVectorField
from hare.dialects.postgresql.search.criterion.search_arguments import SearchArguments
from hare.dialects.postgresql.search.query.search_query import SearchQuery
from hare.dialects.postgresql.search.types import NormalizationInput, QueryInput, RankWeightInput, VectorInput
from hare.dialects.postgresql.search.vector.search_vector import SearchVector
from hare.exceptions import FieldError
from hare.fields import FloatField
from hare.fields.generated import GeneratedField
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult, F
from hare.query.lookup_paths import LookupPaths
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql.terms.base.term import Term
from hare.sql.terms.functions.function import Function as HareSqlFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class SearchRank(Expression):
    """Builds a ``TS_RANK``/``TS_RANK_CD`` expression scoring how well a tsvector matches a
    tsquery, for use in an annotation (e.g. to order results by relevance).

    Args:
        vector: A `SearchVector`/tsvector term, or a field name to wrap in one.
        query: A `SearchQuery`/tsquery term, or search text to wrap in one.
        weights: Per-label (D,C,B,A) weights, as a 4-element sequence or an expression.
        normalization: A bitmask (or expression) selecting `TS_RANK`'s length-normalization
            behaviour.
        cover_density: Use ``TS_RANK_CD`` instead of ``TS_RANK``.
    """

    def __init__(
        self,
        vector: VectorInput,
        query: QueryInput,
        weights: RankWeightInput | None = None,
        normalization: NormalizationInput | None = None,
        cover_density: bool = False,
    ) -> None:
        self.vector = vector
        self.query = query
        self.weights = weights
        self.normalization = normalization
        self.cover_density = cover_density

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """The vector - a field name is part of the structure, read as its own tsvector column
        or wrapped in ``SearchVector(...)`` by the model the key holds - the query, the weights and
        the normalization, in the order ``get_result()`` resolves them.

        Args:
            context: The context the rank is resolved in.

        Returns:
            The description, None for an argument given as a SQL term.
        """
        vector_argument: Any = self.vector if isinstance(self.vector, (Expression, Term)) else F(self.vector)
        query_argument: Any = self.query if isinstance(self.query, (Expression, Term)) else SearchQuery(self.query)
        return PlanDescription.combine(
            (SearchRank, self.cover_density),
            (
                PlanDescription.ABSENT
                if argument is None
                else SearchArguments.get_plan_description(argument, context, treat_str_as_field=False)
                for argument in (vector_argument, query_argument, self.weights, self.normalization)
            ),
        )

    @staticmethod
    def _names_a_stored_tsvector_field(name: str, model: type[Model]) -> bool:
        """Whether ``name`` - a field name, possibly across a relation - is a stored ``TSVectorField``
        column, a ``GeneratedField`` of one included. A path that doesn't resolve isn't one.
        """
        try:
            terminal_field = LookupPaths.expand_expression(model, name)[-1]
        except FieldError:
            return False
        effective_field = terminal_field.output_field if isinstance(terminal_field, GeneratedField) else terminal_field
        return isinstance(effective_field, TSVectorField)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        vector_expr: VectorInput
        if isinstance(self.vector, (Expression, Term)):
            vector_expr = self.vector
        elif isinstance(self.vector, str) and self._names_a_stored_tsvector_field(
            self.vector, expression_context.model
        ):
            # A stored tsvector column is used as is - TO_TSVECTOR() takes no tsvector.
            vector_expr = F(self.vector)
        else:
            vector_expr = SearchVector(self.vector)
        query_expr = self.query if isinstance(self.query, (Expression, Term)) else SearchQuery(self.query)
        vector_result = SearchArguments.get_result(vector_expr, expression_context, treat_str_as_field=False)
        query_result = SearchArguments.get_result(query_expr, expression_context, treat_str_as_field=False)

        args = [vector_result.term, query_result.term]
        joins = ExpressionResult.dedup_joins(vector_result.joins, query_result.joins)

        if self.weights is not None:
            weights_result = SearchArguments.get_result(self.weights, expression_context, treat_str_as_field=False)
            args = [weights_result.term, *args]
            joins = ExpressionResult.dedup_joins(joins, weights_result.joins)

        if self.normalization is not None:
            normalization_result = SearchArguments.get_result(
                self.normalization, expression_context, treat_str_as_field=False
            )
            args.append(normalization_result.term)
            joins = ExpressionResult.dedup_joins(joins, normalization_result.joins)

        function = "TS_RANK_CD" if self.cover_density else "TS_RANK"
        term = HareSqlFunction(function, *args)
        return ExpressionResult(term=term, joins=joins, output_field=FloatField())
