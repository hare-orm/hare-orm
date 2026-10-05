from __future__ import annotations

from typing import Any

from hare.dialects.postgresql.functions.statistics.statistic_pair_aggregate import StatisticPairAggregate
from hare.fields.field import Field
from hare.query.expressions import (
    ExpressionResult,
)
from hare.query.expressions.numeric.numeric_typing import NumericTyping


class RegrCount(StatisticPairAggregate):
    """REGR_COUNT(y, x) - the number of rows where both are non-NULL."""

    function_name = "REGR_COUNT"

    def _get_output_field(
        self, function_arg: ExpressionResult, default_results: list[ExpressionResult]
    ) -> Field[Any] | None:
        return NumericTyping.INTEGER_OUTPUT_FIELD  # type: ignore[arg-type]
