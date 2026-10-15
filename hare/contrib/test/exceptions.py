from __future__ import annotations


class RollbackIsolationEnd(Exception):
    """What ends a transaction isolating a test - it rolls the transaction back."""
