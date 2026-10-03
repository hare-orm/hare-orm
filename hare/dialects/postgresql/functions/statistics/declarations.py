from hare.dialects.postgresql.functions.statistics.statistic_pair_aggregate import StatisticPairAggregate
from hare.utils.declared_subclass import DeclaredSubclass

Corr = DeclaredSubclass.make(
    StatisticPairAggregate,
    "Corr",
    __package__,
    """CORR(y, x) - the correlation coefficient.""",
    function_name="CORR",
)


RegrAvgX = DeclaredSubclass.make(
    StatisticPairAggregate,
    "RegrAvgX",
    __package__,
    """REGR_AVGX(y, x) - the average of the independent variable.""",
    function_name="REGR_AVGX",
)


RegrAvgY = DeclaredSubclass.make(
    StatisticPairAggregate,
    "RegrAvgY",
    __package__,
    """REGR_AVGY(y, x) - the average of the dependent variable.""",
    function_name="REGR_AVGY",
)


RegrIntercept = DeclaredSubclass.make(
    StatisticPairAggregate,
    "RegrIntercept",
    __package__,
    """REGR_INTERCEPT(y, x) - the y-intercept of the least-squares line.""",
    function_name="REGR_INTERCEPT",
)


RegrR2 = DeclaredSubclass.make(
    StatisticPairAggregate,
    "RegrR2",
    __package__,
    """REGR_R2(y, x) - the square of the correlation coefficient.""",
    function_name="REGR_R2",
)


RegrSlope = DeclaredSubclass.make(
    StatisticPairAggregate,
    "RegrSlope",
    __package__,
    """REGR_SLOPE(y, x) - the slope of the least-squares line.""",
    function_name="REGR_SLOPE",
)


RegrSXX = DeclaredSubclass.make(
    StatisticPairAggregate,
    "RegrSXX",
    __package__,
    """REGR_SXX(y, x) - the sum of squares of the independent variable.""",
    function_name="REGR_SXX",
)


RegrSXY = DeclaredSubclass.make(
    StatisticPairAggregate,
    "RegrSXY",
    __package__,
    """REGR_SXY(y, x) - the sum of products of the independent and dependent variables.""",
    function_name="REGR_SXY",
)


RegrSYY = DeclaredSubclass.make(
    StatisticPairAggregate,
    "RegrSYY",
    __package__,
    """REGR_SYY(y, x) - the sum of squares of the dependent variable.""",
    function_name="REGR_SYY",
)
