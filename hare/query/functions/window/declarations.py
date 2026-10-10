from __future__ import annotations

from hare.classes.declared_subclass import DeclaredSubclass
from hare.query import functions
from hare.query.functions.window.distribution_window_function import DistributionWindowFunction
from hare.query.functions.window.field_window_function import FieldWindowFunction
from hare.query.functions.window.offset_window_function import OffsetWindowFunction
from hare.query.functions.window.rank_like_window_function import RankLikeWindowFunction
from hare.query.functions.window.statistic_window_function import StatisticWindowFunction
from hare.sql.enums import AnalyticFunctionName
from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction

RowNumber = DeclaredSubclass.make(
    RankLikeWindowFunction,
    "RowNumber",
    __package__,
    """``ROW_NUMBER()`` - the 1-based position of a row within its window partition.""",
    function_name=AnalyticFunctionName.ROW_NUMBER,
)


Rank = DeclaredSubclass.make(
    RankLikeWindowFunction,
    "Rank",
    __package__,
    """``RANK()`` - position within the partition, leaving gaps after ties.""",
    function_name=AnalyticFunctionName.RANK,
)


DenseRank = DeclaredSubclass.make(
    RankLikeWindowFunction,
    "DenseRank",
    __package__,
    """``DENSE_RANK()`` - like ``Rank()``, but without gaps after ties.""",
    function_name=AnalyticFunctionName.DENSE_RANK,
)


Sum = DeclaredSubclass.make(
    FieldWindowFunction,
    "Sum",
    __package__,
    """``SUM(field)`` computed over the window.""",
    function_name=AnalyticFunctionName.SUM,
    analytic_class=WindowFrameAnalyticFunction,
    aggregate_class=functions.Sum,
)


Max = DeclaredSubclass.make(
    FieldWindowFunction,
    "Max",
    __package__,
    """``MAX(field)`` computed over the window.""",
    function_name=AnalyticFunctionName.MAX,
    analytic_class=WindowFrameAnalyticFunction,
    aggregate_class=functions.Max,
)


Min = DeclaredSubclass.make(
    FieldWindowFunction,
    "Min",
    __package__,
    """``MIN(field)`` computed over the window.""",
    function_name=AnalyticFunctionName.MIN,
    analytic_class=WindowFrameAnalyticFunction,
    aggregate_class=functions.Min,
)


FirstValue = DeclaredSubclass.make(
    FieldWindowFunction,
    "FirstValue",
    __package__,
    """``FIRST_VALUE(field)`` - the field's value on the window's first row.""",
    function_name=AnalyticFunctionName.FIRST_VALUE,
    analytic_class=WindowFrameAnalyticFunction,
    accepts_encrypted_field=True,
)


LastValue = DeclaredSubclass.make(
    FieldWindowFunction,
    "LastValue",
    __package__,
    """``LAST_VALUE(field)`` - the field's value on the window's last row.

    Always evaluated over the whole partition (an explicit ``ROWS BETWEEN UNBOUNDED PRECEDING AND
    UNBOUNDED FOLLOWING`` frame), not just the rows up to the current one - SQL's own default frame when
    ``order_by`` is set (``RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW``) would otherwise make
    this always equal the CURRENT row's value instead of the partition's actual last row.""",
    function_name=AnalyticFunctionName.LAST_VALUE,
    analytic_class=WindowFrameAnalyticFunction,
    accepts_encrypted_field=True,
    full_partition_frame=True,
)


Lag = DeclaredSubclass.make(
    OffsetWindowFunction,
    "Lag",
    __package__,
    """``LAG(field, offset, default)`` - the field's value ``offset`` rows before the current one.

    Args:
        field: Field to read the value from.
        offset: Number of rows before the current one to look back.
        default: Value to use when there is no such row.""",
    function_name=AnalyticFunctionName.LAG,
)


Lead = DeclaredSubclass.make(
    OffsetWindowFunction,
    "Lead",
    __package__,
    """``LEAD(field, offset, default)`` - the field's value ``offset`` rows after the current one.

    Args:
        field: Field to read the value from.
        offset: Number of rows after the current one to look ahead.
        default: Value to use when there is no such row.""",
    function_name=AnalyticFunctionName.LEAD,
)


StdDev = DeclaredSubclass.make(
    StatisticWindowFunction,
    "StdDev",
    __package__,
    """The standard deviation over the window.""",
    function_names=("STDDEV_POP", "STDDEV_SAMP"),
    aggregate_class=functions.StdDev,
)


Variance = DeclaredSubclass.make(
    StatisticWindowFunction,
    "Variance",
    __package__,
    """The variance over the window.""",
    function_names=("VAR_POP", "VAR_SAMP"),
    aggregate_class=functions.Variance,
)


CumeDist = DeclaredSubclass.make(
    DistributionWindowFunction,
    "CumeDist",
    __package__,
    """``CUME_DIST()`` - the share of the partition's rows ordered up to and including this row's peers.""",
    function_name=AnalyticFunctionName.CUME_DIST,
)


PercentRank = DeclaredSubclass.make(
    DistributionWindowFunction,
    "PercentRank",
    __package__,
    """``PERCENT_RANK()`` - ``(rank - 1) / (rows - 1)``, 0 for a one-row partition.""",
    function_name=AnalyticFunctionName.PERCENT_RANK,
)
