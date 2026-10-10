from __future__ import annotations


class HareLoopSwitchWarning(UserWarning):
    """A connection found its event loop changed - Hare creates a fresh connection for the new loop.
    Expected in tests (``hare_test_context()`` silences it); in production it usually means a bug.
    """


class RedundantDbDefaultWarning(UserWarning):
    """A field defines both ``default`` and ``db_default`` - ``default`` is always assigned before the
    INSERT, so ``db_default`` never applies.
    """
