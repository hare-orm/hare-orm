"""Transactions: ``Transactions.atomic()`` and its options, distributed (two-phase) transactions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hare.classes.lazy_exports import LazyExports
from hare.transactions.constants import EXPORTED_MODULES

if TYPE_CHECKING:
    from hare.transactions.atomic.atomic import Atomic
    from hare.transactions.distributed.distributed_recovery import DistributedRecovery
    from hare.transactions.distributed.distributed_transactions import DistributedTransactions
    from hare.transactions.enums import IsolationLevel, TransactionEventType, TransactionOutcome, TransactionPhase
    from hare.transactions.transactions import Transactions

__all__ = [
    "Atomic",
    "DistributedTransactions",
    "DistributedRecovery",
    "IsolationLevel",
    "TransactionEventType",
    "TransactionOutcome",
    "TransactionPhase",
    "Transactions",
]


__getattr__, __dir__ = LazyExports(__name__, EXPORTED_MODULES).get_module_hooks()
