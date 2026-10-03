import contextvars
import itertools
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.dialects.base.nested_savepoint_lock import NestedSavepointLock


class SavepointSpan:
    """Opaque token identifying one open SAVEPOINT's lifetime, for ``NestedSavepointLock``'s own
    bookkeeping - never compared by anything but identity. Carries a small counter purely so a
    failed test/assertion prints something legible instead of a bare ``object()`` address."""

    _counter = itertools.count()

    __slots__ = ("_id", "owner_lock", "parent", "released", "statement_interrupted")

    def __init__(self, owner_lock: "NestedSavepointLock", parent: "SavepointSpan | None" = None) -> None:
        self._id = next(SavepointSpan._counter)
        #: The lock that created this span - tells a span of another connection's lock from an
        #: ancestor.
        self.owner_lock = owner_lock
        self.parent = parent
        self.released = False
        #: A statement run under this savepoint was cancelled while in flight.
        self.statement_interrupted = False

    def __repr__(self) -> str:
        return f"<SavepointSpan {self._id}>"

    def is_within(self, savepoint_span: "SavepointSpan") -> bool:
        """Whether this span is ``savepoint_span`` itself or was opened (directly or not) under it.

        Args:
            savepoint_span: the span that may enclose this one.
        """
        span: SavepointSpan | None = self
        while span is not None:
            if span is savepoint_span:
                return True
            span = span.parent
        return False


#: The span this execution context is nested under, None at the top-level transaction. A ContextVar,
#: so a task spawned inside an open span inherits it and is seen as its descendant.
current_savepoint_span: contextvars.ContextVar[SavepointSpan | None] = contextvars.ContextVar(
    "current_savepoint_span", default=None
)
