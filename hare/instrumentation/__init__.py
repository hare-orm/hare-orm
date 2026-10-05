"""Observing what the ORM does: ``Observers`` - the one registry of observers - and the events
they get (``QueryExecuted``, ``TransactionEvent``, ``RowsChanged``), query wrappers
(``QueryWrapper``) and query tags (``QueryTags``)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hare.classes.lazy_exports import LazyExports
from hare.instrumentation.constants import EXPORTED_MODULES

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.instrumentation.declarations import QueryCall, QueryExecuted, RowsChanged, TransactionEvent
    from hare.instrumentation.enums import RowOperation
    from hare.instrumentation.observers.observers import Observers
    from hare.instrumentation.queries.query_tags import QueryTags
    from hare.instrumentation.queries.query_wrapper import QueryWrapper

__all__ = [
    "Observers",
    "QueryCall",
    "QueryExecuted",
    "QueryTags",
    "QueryWrapper",
    "RowOperation",
    "RowsChanged",
    "TransactionEvent",
]


__getattr__ = LazyExports(__name__, EXPORTED_MODULES).get
