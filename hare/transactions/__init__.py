"""Transactions: ``Transactions.atomic()`` and its options, distributed (two-phase) transactions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hare.classes.lazy_exports import LazyExports
from hare.transactions.constants import EXPORTED_MODULES

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.transactions.atomic.atomic import Atomic
    from hare.transactions.distributed.distributed_transactions import DistributedTransactions
    from hare.transactions.enums import IsolationLevel, TransactionEventType
    from hare.transactions.transactions import Transactions

__all__ = [
    "Atomic",
    "DistributedTransactions",
    "IsolationLevel",
    "TransactionEventType",
    "Transactions",
]


__getattr__ = LazyExports(__name__, EXPORTED_MODULES).get
