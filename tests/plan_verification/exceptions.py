from __future__ import annotations


class PlanMismatchError(AssertionError):
    """A query ran on a kept plan whose SQL or parameters differ from the query built in full."""
