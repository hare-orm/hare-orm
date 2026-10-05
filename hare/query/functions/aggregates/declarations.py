from __future__ import annotations

from hare.classes.declared_subclass import DeclaredSubclass
from hare.query.expressions import Aggregate
from hare.query.expressions.compatibility.constants import UNORDERED_VALUE_TYPES
from hare.query.functions.aggregates.constants import KEYED_MAX_FUNCTION_NAME, KEYED_MIN_FUNCTION_NAME
from hare.query.functions.aggregates.statistic_aggregate import StatisticAggregate

Max = DeclaredSubclass.make(
    Aggregate,
    "Max",
    __package__,
    """Returns largest value in the column, e.g. ``Max("field_name")``.""",
    function_name="MAX",
    keyed_function_name=KEYED_MAX_FUNCTION_NAME,
    keeps_argument_collation=True,
    refused_argument_value_types=UNORDERED_VALUE_TYPES,
    reads_extreme_value=True,
    populate_field_object=True,
    ignores_repeated_rows=True,
)


Min = DeclaredSubclass.make(
    Aggregate,
    "Min",
    __package__,
    """Returns smallest value in the column, e.g. ``Min("field_name")``.""",
    function_name="MIN",
    keyed_function_name=KEYED_MIN_FUNCTION_NAME,
    keeps_argument_collation=True,
    refused_argument_value_types=UNORDERED_VALUE_TYPES,
    reads_extreme_value=True,
    populate_field_object=True,
    ignores_repeated_rows=True,
)


StdDev = DeclaredSubclass.make(
    StatisticAggregate,
    "StdDev",
    __package__,
    """The standard deviation: ``StdDev("salary")``, ``StdDev("salary", sample=True)``.""",
    function_names=("STDDEV_POP", "STDDEV_SAMP"),
)


Variance = DeclaredSubclass.make(
    StatisticAggregate,
    "Variance",
    __package__,
    """The variance: ``Variance("salary")``, ``Variance("salary", sample=True)``.""",
    function_names=("VAR_POP", "VAR_SAMP"),
)
