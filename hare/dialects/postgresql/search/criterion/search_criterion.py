from hare.dialects.postgresql.search.criterion.declarations import Comp, TsInfixOperator
from hare.dialects.postgresql.search.criterion.ts_query_function import TsQueryFunction
from hare.dialects.postgresql.search.criterion.ts_query_invert import TsQueryInvert
from hare.dialects.postgresql.search.functions.plain_to_ts_query import PlainToTsQuery
from hare.dialects.postgresql.search.functions.to_ts_vector import ToTsVector
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.criteria.basic_criterion import BasicCriterion


class SearchCriterion(BasicCriterion):
    """A ``tsvector @@ tsquery`` filter criterion, as used by the ``search`` field lookup.

    Args:
        field: The column (or already-built tsvector term) to match against.
        expr: The search text, or an already-built tsquery term.
        vectorize: If True, wraps `field` in ``TO_TSVECTOR()``; set False when `field` is
            already a tsvector (e.g. a `TSVectorField`).
        config: Text search configuration name (e.g. a `TSVectorField`'s own configured
            language) - without it, TO_TSVECTOR()/PLAINTO_TSQUERY() both fall back to Postgres's
            session default_text_search_config, which silently mismatches a column whose stored
            vector was built with a DIFFERENT config (e.g. "russian"). Only applied to the pieces
            THIS constructor itself builds - an already-built `expr` Term is used as-is, and when
            no config is given, the vectorized column uses that query's own configuration.
    """

    def __init__(
        self, field: Term, expr: Term | str, vectorize: bool = True, config: str | Term | None = None
    ) -> None:
        vector_config = config
        if vector_config is None and isinstance(expr, Term):
            vector_config = self.get_query_config(expr)
        vector = ToTsVector(field, config=vector_config) if vectorize else field
        query = expr if isinstance(expr, Term) else PlainToTsQuery(ValueWrapper(expr), config=config)
        super().__init__(Comp.search, vector, query)

    @classmethod
    def get_query_config(cls, query: Term) -> Term | None:
        """The text search configuration an already-built tsquery term was built with.

        Args:
            query: The tsquery term.

        Returns:
            The configuration term, or None when the query has none (or its combined parts
            disagree on one).
        """
        if isinstance(query, TsQueryFunction):
            return query.config_term
        if isinstance(query, TsQueryInvert):
            return cls.get_query_config(query.term)
        if isinstance(query, TsInfixOperator):
            left_config = cls.get_query_config(query.left)
            right_config = cls.get_query_config(query.right)
            if left_config is None or right_config is None:
                return None
            if isinstance(left_config, ValueWrapper) and isinstance(right_config, ValueWrapper):
                return left_config if left_config.value == right_config.value else None
            return left_config if left_config is right_config else None
        return None
