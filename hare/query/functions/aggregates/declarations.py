from __future__ import annotations

from hare.classes.declared_subclass import DeclaredSubclass
from hare.query.expressions import Aggregate
from hare.query.functions.aggregates.statistic_aggregate import StatisticAggregate

Sum = DeclaredSubclass.make(
    Aggregate,
    "Sum",
    __package__,
    """Adds up all the values for that column, e.g. ``Sum("field_name")``.""",
    function_name="SUM",
    populate_field_object=True,
)


Max = DeclaredSubclass.make(
    Aggregate,
    "Max",
    __package__,
    """Returns largest value in the column, e.g. ``Max("field_name")``.""",
    function_name="MAX",
    keeps_argument_collation=True,
    populate_field_object=True,
    ignores_repeated_rows=True,
)


Min = DeclaredSubclass.make(
    Aggregate,
    "Min",
    __package__,
    """Returns smallest value in the column, e.g. ``Min("field_name")``.""",
    function_name="MIN",
    keeps_argument_collation=True,
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
