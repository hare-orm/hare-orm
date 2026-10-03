"""Transactions: ``Transactions.atomic()`` and its options, distributed (two-phase) transactions."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.transactions.atomic import Atomic
    from hare.transactions.distributed.distributed_coordinator import DistributedCoordinator
    from hare.transactions.distributed.distributed_transactions import DistributedTransactions
    from hare.transactions.enums import IsolationLevel, TransactionEventType
    from hare.transactions.options import TransactionOptions
    from hare.transactions.transactions import Transactions

__all__ = [
    "Atomic",
    "DistributedCoordinator",
    "DistributedTransactions",
    "IsolationLevel",
    "TransactionEventType",
    "TransactionOptions",
    "Transactions",
]

#: The module of each exported name - imported on first use: the database clients import modules
#: of this package while ``Transactions`` builds on the clients.
EXPORTED_MODULES = {
    "Atomic": "hare.transactions.atomic",
    "DistributedCoordinator": "hare.transactions.distributed",
    "DistributedTransactions": "hare.transactions.distributed",
    "IsolationLevel": "hare.transactions.enums",
    "TransactionEventType": "hare.transactions.enums",
    "TransactionOptions": "hare.transactions.options",
    "Transactions": "hare.transactions.transactions",
}


def __getattr__(name: str) -> Any:
    module_name = EXPORTED_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module_name), name)
