from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.classes.value_descriptions import ValueDescriptions
from hare.dialects.base.search.enums import TextSearchOperation
from hare.dialects.base.search.text_search import TextSearch
from hare.dialects.postgresql.fields.ts_vector_field import TSVectorField
from hare.dialects.postgresql.search.constants import (
    POSTGRESQL_HEADLINE_OPTION_NAMES,
    POSTGRESQL_INTEGER_RANGE,
    POSTGRESQL_RANK_LABEL_WEIGHT_COUNT,
    POSTGRESQL_SEARCH_CONFIG_SQL_TYPE,
    POSTGRESQL_SEARCH_QUERY_OPERATORS,
    SEARCH_TYPE_FUNCTIONS,
)
from hare.dialects.postgresql.search.criterion.declarations import TsInfixOperator
from hare.dialects.postgresql.search.criterion.ts_query_function import TsQueryFunction
from hare.dialects.postgresql.search.criterion.ts_query_invert import TsQueryInvert
from hare.dialects.postgresql.search.enums import TsWeight
from hare.exceptions import ConfigurationError, FieldError, QueryError
from hare.fields import FloatField, TextField
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.fields.generated_field import GeneratedField
from hare.numbers.finite_numbers import FiniteNumbers
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult, F
from hare.query.expressions.expression_result import TableCriterionTuple
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.lookup_info.lookup_paths import LookupPaths
from hare.search.search_arguments import SearchArguments
from hare.search.vector.search_vector import SearchVector
from hare.sql.functions.cast import Cast
from hare.sql.functions.coalesce import Coalesce
from hare.sql.terms.functions.function import Function as HareSqlFunction
from hare.sql.terms.term import Term
from hare.sql.terms.values.literal_value import LiteralValue
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.models.model_binding import ModelBinding
    from hare.search import CombinedSearchQuery, CombinedSearchVector, SearchHeadline, SearchQuery, SearchRank
    from hare.search.types import HeadlineOptionValue, VectorInput


class PostgresqlTextSearch(TextSearch):
    """PostgreSQL's full-text search - tsvectors (``TO_TSVECTOR``), tsqueries (``PLAINTO_TSQUERY``
    and its kin), ``TS_RANK``/``TS_RANK_CD`` and ``TS_HEADLINE``."""

    @staticmethod
    def get_config_result(
        expression: SearchVector | SearchQuery | SearchHeadline, expression_context: ExpressionContext
    ) -> ExpressionResult:
        """Bind literal and expression configurations using PostgreSQL's regconfig type."""
        result = SearchArguments.get_result(expression, "config", expression_context)
        return ExpressionResult(
            term=Cast(result.term, LiteralValue(POSTGRESQL_SEARCH_CONFIG_SQL_TYPE)), joins=result.joins
        )

    def get_query_result(self, query: SearchQuery, expression_context: ExpressionContext) -> ExpressionResult:
        self.check_query(query, expression_context)
        value_result = SearchArguments.get_result(query, "value", expression_context)
        joins = value_result.joins
        config_term = None
        if query.config is not None:
            config_result = self.get_config_result(query, expression_context)
            config_term = config_result.term
            joins = ExpressionResult.dedup_joins(joins, config_result.joins)
        term: Term = TsQueryFunction(SEARCH_TYPE_FUNCTIONS[query.search_type], value_result.term, config_term)
        if query.invert:
            term = TsQueryInvert(term)
        return ExpressionResult(term=term, joins=joins)

    def get_combined_query_result(
        self, query: CombinedSearchQuery, expression_context: ExpressionContext
    ) -> ExpressionResult:
        left = query.left.get_result(expression_context)
        right = query.right.get_result(expression_context)
        term: Term = TsInfixOperator(left.term, POSTGRESQL_SEARCH_QUERY_OPERATORS[query.operator], right.term)
        if query.negated:
            term = TsQueryInvert(term)
        return ExpressionResult(term=term, joins=ExpressionResult.dedup_joins(left.joins, right.joins))

    def get_vector_result(self, vector: SearchVector, expression_context: ExpressionContext) -> ExpressionResult:
        self.check_vector(vector, expression_context)
        expression_results = [
            ExpressionArguments.get_field_result(vector, "expressions", expression, expression_context, index)
            for index, expression in enumerate(vector.expressions)
        ]
        terms: list[Term] = []
        for expression, expression_result in zip(vector.expressions, expression_results, strict=True):
            source_field = (
                expression.get_value_field(expression_result)
                if isinstance(expression, Expression)
                else expression_result.output_field
            )
            EncryptedFieldBase.raise_if_encrypted(source_field, "SearchVector()")
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
        if vector.config is not None:
            config_result = self.get_config_result(vector, expression_context)
            args = [config_result.term, combined]
            joins = ExpressionResult.dedup_joins(joins, config_result.joins)

        vector_term = HareSqlFunction("TO_TSVECTOR", *args)

        if vector.weight is not None:
            if isinstance(vector.weight, str):
                if vector.weight.upper() not in TsWeight:
                    raise QueryError(f"SearchVector weight must be one of A, B, C, D, got {vector.weight!r}")
                # setweight()'s weight is of type "char": a text parameter is rejected, a string
                # literal isn't.
                weight_term: Term = LiteralValue(f"'{TsWeight(vector.weight.upper()).value}'")
                weight_joins: list[TableCriterionTuple] = []
            else:
                weight_result = SearchArguments.get_result(vector, "bound_weight", expression_context)
                # Cast through TEXT, so a bound parameter is typed as text rather than "char"
                # (which a driver can't bind from a str).
                weight_term = Cast(Cast(weight_result.term, "TEXT"), LiteralValue('"char"'))
                weight_joins = weight_result.joins
            vector_term = HareSqlFunction("SETWEIGHT", vector_term, weight_term)
            joins = ExpressionResult.dedup_joins(joins, weight_joins)

        return ExpressionResult(term=vector_term, joins=joins, output_field=TSVectorField())

    def get_combined_vector_result(
        self, vector: CombinedSearchVector, expression_context: ExpressionContext
    ) -> ExpressionResult:
        self.require_operation(
            expression_context, TextSearchOperation.VECTOR_CONCATENATION, "SearchVector + SearchVector"
        )
        left = vector.left.get_result(expression_context)
        right = vector.right.get_result(expression_context)
        # Each side is already a NULL-safe TO_TSVECTOR(...) - SearchVector guards its own sources with
        # COALESCE.
        term = TsInfixOperator(left.term, " || ", right.term)
        return ExpressionResult(
            term=term,
            joins=ExpressionResult.dedup_joins(left.joins, right.joins),
            output_field=TSVectorField(),
        )

    @staticmethod
    def names_a_stored_tsvector_field(name: str, binding: ModelBinding) -> bool:
        """Whether a field name - possibly across a relation - is a stored ``TSVectorField``
        column, a ``GeneratedField`` of one included.

        Args:
            name: The field name.
            binding: The model the name starts from.

        Returns:
            True for such a column; False for a path that doesn't resolve.
        """
        try:
            _, terminal_field = LookupPaths.get_expression_path(binding, name)
        except FieldError:
            return False
        return isinstance(GeneratedField.get_effective_field(terminal_field), TSVectorField)

    def get_rank_result(self, rank: SearchRank, expression_context: ExpressionContext) -> ExpressionResult:
        self.check_rank(rank, expression_context)
        vector_expression: VectorInput
        if isinstance(rank.vector, (Expression, Term)):
            vector_expression = rank.vector
        elif isinstance(rank.vector, str) and self.names_a_stored_tsvector_field(
            rank.vector, expression_context.binding
        ):
            # A stored tsvector column is used as is - TO_TSVECTOR() takes no tsvector.
            vector_expression = F(rank.vector)
        elif isinstance(rank.vector, str):
            vector_expression = SearchVector(rank.vector)
        else:
            vector_expression = SearchVector(*rank.vector)
        vector_result = (
            ExpressionResult(term=vector_expression)
            if isinstance(vector_expression, Term) and not isinstance(vector_expression, Expression)
            else vector_expression.get_result(expression_context)
        )
        query_result = SearchArguments.get_result(rank, "query_argument", expression_context)

        args = [vector_result.term, query_result.term]
        joins = ExpressionResult.dedup_joins(vector_result.joins, query_result.joins)

        if rank.bound_weights is not None:
            weights_result = SearchArguments.get_result(
                rank,
                "bound_weights",
                expression_context,
                binds_whole=True,
                literal_check=self.check_rank_weights,
            )
            args = [weights_result.term, *args]
            joins = ExpressionResult.dedup_joins(joins, weights_result.joins)

        if rank.normalization is not None:
            normalization_result = SearchArguments.get_result(
                rank,
                "normalization",
                expression_context,
                literal_check=self.check_rank_normalization,
            )
            args.append(normalization_result.term)
            joins = ExpressionResult.dedup_joins(joins, normalization_result.joins)

        function = "TS_RANK_CD" if rank.cover_density else "TS_RANK"
        term = HareSqlFunction(function, *args)
        return ExpressionResult(term=term, joins=joins, output_field=FloatField())

    @staticmethod
    def check_rank_weights(weights: Any) -> None:
        """Check each literal weight array, including later values bound to a stored plan."""
        if weights is not None and not isinstance(weights, (str, Expression, Term)):
            if (
                not isinstance(weights, Sequence)
                or len(weights) != POSTGRESQL_RANK_LABEL_WEIGHT_COUNT
                or not all(FiniteNumbers.is_finite_number(weight) for weight in weights)
            ):
                raise ConfigurationError(
                    f"SearchRank weights must be {POSTGRESQL_RANK_LABEL_WEIGHT_COUNT} numbers - the weights of the "
                    f"D, C, B and A labels - or field weights by name, got {weights!r}"
                )

    @staticmethod
    def check_rank_normalization(normalization: Any) -> None:
        """Check the normalization bitmask on both initial and prepared binding."""
        if normalization is None or isinstance(normalization, (Expression, Term)):
            return
        if (
            isinstance(normalization, bool)
            or not isinstance(normalization, int)
            or not POSTGRESQL_INTEGER_RANGE[0] <= normalization <= POSTGRESQL_INTEGER_RANGE[1]
        ):
            raise ConfigurationError(
                f"SearchRank normalization must be an integer bitmask of 32 bits, got "
                f"{ValueDescriptions.get_shown_value(normalization)}"
            )

    def get_headline_result(self, headline: SearchHeadline, expression_context: ExpressionContext) -> ExpressionResult:
        self.check_headline(headline, expression_context)
        expression_result = ExpressionArguments.get_field_result(
            headline, "expression", headline.expression, expression_context
        )
        query_result = SearchArguments.get_result(headline, "query_argument", expression_context)

        args = [expression_result.term, query_result.term]
        joins = ExpressionResult.dedup_joins(expression_result.joins, query_result.joins)

        if headline.config is not None:
            config_result = self.get_config_result(headline, expression_context)
            args = [config_result.term, *args]
            joins = ExpressionResult.dedup_joins(joins, config_result.joins)

        options = headline.get_options()
        if options:
            options_sql = ", ".join(
                f"{POSTGRESQL_HEADLINE_OPTION_NAMES[name]}={self.format_option_value(value)}"
                for name, value in options.items()
            )
            args.append(ValueWrapper(options_sql))

        term = HareSqlFunction("TS_HEADLINE", *args)
        return ExpressionResult(term=term, joins=joins, output_field=TextField())

    @staticmethod
    def format_option_value(value: HeadlineOptionValue) -> str:
        """A ``ts_headline()`` option's value as its options text writes it.

        Args:
            value: The value.

        Returns:
            ``true``/``false``, a quoted text or a number.
        """
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
        return str(value)
