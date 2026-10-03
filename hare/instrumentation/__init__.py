"""Observing what the ORM does: ``Observers`` - the one registry of observers - and the events
they get (``QueryExecuted``, ``TransactionEvent``, ``RowsChanged``), query wrappers
(``QueryWrapper``) and query tags (``QueryTags``)."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.instrumentation.change_events import ChangeEvents
    from hare.instrumentation.enums import RowOperation
    from hare.instrumentation.observers import Observers
    from hare.instrumentation.query_call import QueryCall
    from hare.instrumentation.query_executed import QueryExecuted
    from hare.instrumentation.query_tags import QueryTags
    from hare.instrumentation.query_wrapper import QueryWrapper
    from hare.instrumentation.rows_changed import RowsChanged
    from hare.instrumentation.transaction_event import TransactionEvent

__all__ = [
    "ChangeEvents",
    "Observers",
    "QueryCall",
    "QueryExecuted",
    "QueryTags",
    "QueryWrapper",
    "RowOperation",
    "RowsChanged",
    "TransactionEvent",
]

#: The module of each exported name - imported on first use: the database clients import modules
#: of this package while ``Observers`` builds on the clients' package.
EXPORTED_MODULES = {
    "ChangeEvents": "hare.instrumentation.change_events",
    "Observers": "hare.instrumentation.observers",
    "QueryCall": "hare.instrumentation.query_call",
    "QueryExecuted": "hare.instrumentation.query_executed",
    "QueryTags": "hare.instrumentation.query_tags",
    "QueryWrapper": "hare.instrumentation.query_wrapper",
    "RowOperation": "hare.instrumentation.enums",
    "RowsChanged": "hare.instrumentation.rows_changed",
    "TransactionEvent": "hare.instrumentation.transaction_event",
}


def __getattr__(name: str) -> Any:
    module_name = EXPORTED_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module_name), name)
