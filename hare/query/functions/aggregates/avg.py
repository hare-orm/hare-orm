from __future__ import annotations

from typing import ClassVar

from hare.fields.enums import ValueComputation
from hare.query.expressions.compatibility.constants import UNSUMMED_VALUE_TYPES
from hare.query.functions.aggregates.statistic_result_aggregate import StatisticResultAggregate


class Avg(StatisticResultAggregate):
    """The mean of a group's values, NULLs left out: ``Avg("price")``.
    The database determines the result for an empty group.
    An average of integers or Decimals is an exact Decimal where the database computes one
    (``Features.computes_integer_statistics_exactly``, ``computes_decimal_statistics_exactly``), else a
    float."""

    function_name = "AVG"
    refused_argument_value_types = UNSUMMED_VALUE_TYPES
    computation: ClassVar[ValueComputation] = ValueComputation.AVERAGE
