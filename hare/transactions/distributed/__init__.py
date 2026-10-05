"""Distributed transactions: DistributedTransactions commits one transaction across several
databases with two-phase commit (``Features.supports_two_phase_commit``), and StalePreparedTransaction
describes a prepared transaction recovery finds left behind."""

from __future__ import annotations

from hare.transactions.distributed.distributed_transactions import DistributedTransactions

__all__ = ["DistributedTransactions"]
