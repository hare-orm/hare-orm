from __future__ import annotations

from hare.exceptions import CascadeDepthLimitError


class SqliteTriggerRecursionLimitError(CascadeDepthLimitError):
    """SQLite's ``CascadeDepthLimitError``: a DELETE's ``ON DELETE CASCADE`` recursed past
    ``SQLITE_LIMIT_TRIGGER_DEPTH`` (1000 by default) and SQLite failed the statement.
    ``Model.delete()``/``QuerySet.delete()`` catch it and finish the cascade in Python.
    """
