from hare.sql.functions.cast import Cast
from hare.sql.functions.distinct_option_function import DistinctOptionFunction


class StringAggFunction(DistinctOptionFunction):
    def __init__(self, term, delimiter, alias: str | None = None) -> None:
        super().__init__("STRING_AGG", Cast(term, "TEXT"), delimiter, alias=alias)
