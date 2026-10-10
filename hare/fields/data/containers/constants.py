from __future__ import annotations

import datetime
import uuid

from hare.lazy_loading.lazy_pattern import LazyPattern
from hare.query.enums import Lookup

#: A path segment naming an array element (``tags__0``, 0-indexed; a negative index counts from the
#: end) or a tuple element (``point__1``).
CONTAINER_INDEX_PATH_PATTERN = LazyPattern(r"-?\d+")
#: A path segment naming an array slice (``tags__0_2``, the elements from 0 up to, not including, 2).
ARRAY_SLICE_PATH_PATTERN = LazyPattern(r"(\d+)_(\d+)")
#: A path segment naming an array's length.
ARRAY_LENGTH_PATH_SEGMENT = "len"
#: The path segments reading a map's keys and its values as arrays.
MAP_KEYS_PATH_SEGMENT = "keys"
MAP_VALUES_PATH_SEGMENT = "values"
#: Lookups of an array field whose filter value is a list of elements.
ARRAY_LIST_LOOKUPS = frozenset({Lookup.EXACT, Lookup.NOT, Lookup.CONTAINS, Lookup.CONTAINED_BY, Lookup.OVERLAP})
#: The Python types a map's key is of - values with a stable equality.
MAP_KEY_TYPES = (int, str, uuid.UUID, datetime.date, datetime.datetime)
