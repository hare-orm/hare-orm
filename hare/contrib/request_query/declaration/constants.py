from __future__ import annotations

from hare.query.enums import Lookup

#: The prefix of a method that turns a parameter's value into a condition of its own.
FILTER_METHOD_PREFIX = "filter_"

#: The lookups giving a field's lower bound, and its upper bound - a request's pair of them leaving
#: no value between is refused.
LOWER_BOUND_LOOKUPS = frozenset({Lookup.GT, Lookup.GTE})

UPPER_BOUND_LOOKUPS = frozenset({Lookup.LT, Lookup.LTE})

#: The bound lookups excluding the bound's own value.
STRICT_BOUND_LOOKUPS = frozenset({Lookup.GT, Lookup.LT})

#: The lookups comparing a field's own values - a parameter of ``Meta.filters`` with one of them
#: takes the field's enum, when it has one.
ENUM_VALUE_LOOKUPS = frozenset({Lookup.EXACT, Lookup.NOT, Lookup.IN, Lookup.NOT_IN})
