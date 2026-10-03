from collections.abc import Callable
from typing import Any

from hare.dialects.base.savepoint_span import SavepointSpan


class TransactionCallback:
    """One on_commit()/on_rollback() callback, together with the savepoint span it was registered
    under - a savepoint that rolls back discards exactly the callbacks registered inside it, not
    the ones sibling tasks registered meanwhile at other levels."""

    __slots__ = ("callback", "savepoint_span")

    def __init__(self, callback: Callable[[], Any], savepoint_span: SavepointSpan | None) -> None:
        self.callback = callback
        #: The innermost savepoint open in the registering context, None at the transaction's own level.
        self.savepoint_span = savepoint_span

    def is_registered_within(self, savepoint_span: SavepointSpan) -> bool:
        """Whether this callback was registered inside ``savepoint_span`` (or a savepoint nested in it).

        Args:
            savepoint_span: the savepoint's span.
        """
        return self.savepoint_span is not None and self.savepoint_span.is_within(savepoint_span)
