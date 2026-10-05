"""Parameter types that take their value from one text value: a composite key, a comma-separated list."""

from __future__ import annotations

from hare.contrib.request_query.types.comma_separated import CommaSeparated
from hare.contrib.request_query.types.generic_target import GenericTarget
from hare.contrib.request_query.types.key_columns import KeyColumns
from hare.contrib.request_query.types.text_parameter import TextParameter

__all__ = [
    "TextParameter",
    "KeyColumns",
    "CommaSeparated",
    "GenericTarget",
]
