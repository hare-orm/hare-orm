from __future__ import annotations

from hare.classes.declared_subclass import DeclaredSubclass
from hare.dialects.clickhouse.functions.aggregates.arg_extremum import ArgExtremum
from hare.dialects.clickhouse.functions.aggregates.group_array import GroupArray
from hare.fields.data.numeric.big_int_field import BigIntField
from hare.query.expressions import Aggregate

Uniq = DeclaredSubclass.make(
    Aggregate,
    "Uniq",
    __package__,
    """``uniq(field)`` - about how many distinct values a group has: an estimate from a sample of
    hashes, the fastest count of distinct values.

    Example: ``Event.objects.values("day").annotate(visitors=Uniq("user_id"))``""",
    function_name="uniq",
    computed_over_window=True,
    value_field=BigIntField(),
    ignores_repeated_rows=True,
)

UniqExact = DeclaredSubclass.make(
    Aggregate,
    "UniqExact",
    __package__,
    """``uniqExact(field)`` - how many distinct values a group has, exactly; it keeps every one of
    them in memory.

    Example: ``Event.objects.aggregate(visitors=UniqExact("user_id"))``""",
    function_name="uniqExact",
    computed_over_window=True,
    value_field=BigIntField(),
    ignores_repeated_rows=True,
)

UniqCombined = DeclaredSubclass.make(
    Aggregate,
    "UniqCombined",
    __package__,
    """``uniqCombined(field)`` - about how many distinct values a group has, in less memory and more
    exactly than ``Uniq`` for many of them.

    Example: ``Event.objects.aggregate(visitors=UniqCombined("user_id"))``""",
    function_name="uniqCombined",
    computed_over_window=True,
    value_field=BigIntField(),
    ignores_repeated_rows=True,
)

AnyValue = DeclaredSubclass.make(
    Aggregate,
    "AnyValue",
    __package__,
    """``any(field)`` - the first value of a group ClickHouse meets, whichever it is.

    Example: ``Event.objects.values("user_id").annotate(country=AnyValue("country"))``""",
    function_name="any",
    computed_over_window=True,
    populate_field_object=True,
    ignores_repeated_rows=True,
)

AnyLast = DeclaredSubclass.make(
    Aggregate,
    "AnyLast",
    __package__,
    """``anyLast(field)`` - the last value of a group ClickHouse meets.

    Example: ``Event.objects.values("user_id").annotate(country=AnyLast("country"))``""",
    function_name="anyLast",
    computed_over_window=True,
    populate_field_object=True,
    ignores_repeated_rows=True,
)

ArgMin = DeclaredSubclass.make(
    ArgExtremum,
    "ArgMin",
    __package__,
    """``argMin(field, by)`` - the value of ``field`` in the row of a group where ``by`` is the least.

    Example: ``Quote.objects.values("symbol").annotate(first_price=ArgMin("price", "quoted_at"))``""",
    function_name="argMin",
)

ArgMax = DeclaredSubclass.make(
    ArgExtremum,
    "ArgMax",
    __package__,
    """``argMax(field, by)`` - the value of ``field`` in the row of a group where ``by`` is the greatest.

    Example: ``Quote.objects.values("symbol").annotate(last_price=ArgMax("price", "quoted_at"))``""",
    function_name="argMax",
)

GroupUniqArray = DeclaredSubclass.make(
    GroupArray,
    "GroupUniqArray",
    __package__,
    """``groupUniqArray(field)`` - a group's distinct values as an array, in no order; ``max_size``
    keeps that many of them.

    Example: ``Event.objects.values("user_id").annotate(pages=GroupUniqArray("page"))``""",
    function_name="groupUniqArray",
    ignores_repeated_rows=True,
)

SumMap = DeclaredSubclass.make(
    Aggregate,
    "SumMap",
    __package__,
    """``sumMap(field)`` - the maps of a group added up key by key: a map of every key to the sum of
    its values.

    Example: ``Shipment.objects.aggregate(totals=SumMap("prices"))``""",
    function_name="sumMap",
    computed_over_window=True,
    populate_field_object=True,
)
