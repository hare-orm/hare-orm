import asyncio

from hare.dialects.base.constants import AMBIENT_QUERY_SAVEPOINT_WAIT_TIMEOUT_SECONDS
from hare.dialects.base.savepoint_span import SavepointSpan
from hare.exceptions import TransactionManagementError


class NestedSavepointLock:
    """Coordinates the SAVEPOINT/RELEASE/ROLLBACK TO of concurrent tasks on one connection. SQL
    savepoints close strictly last-in-first-out - RELEASE releases every savepoint opened after the
    named one - so spans that overlap without one being the ancestor of the other would corrupt the
    stack.

    One instance per top-level transaction, shared by its nested clients. It tracks a tree of open
    spans, not which task holds a lock: a new span waits only until the span it was spawned under is
    the topmost open one - so the children of ``gather()`` inside a savepoint serialize against each
    other without waiting for their parent.
    """

    def __init__(self) -> None:
        self._open_spans: list[SavepointSpan] = []
        #: One future per acquire() waiting for its turn, woken by every release().
        self._waiters: list[asyncio.Future[None]] = []

    async def acquire(self, parent_span: SavepointSpan | None, timeout_seconds: float | None = None) -> SavepointSpan:
        """Waits until the nearest still-open ancestor of ``parent_span`` is the topmost open span,
        then opens a new span under it. A released ``parent_span`` is replaced by its parent. When
        the ancestor already is topmost, the span opens without suspending.

        Args:
            parent_span: A span of this lock (``get_own_ancestor_span()``), None at the top level.
            timeout_seconds: How long to wait for the turn; None waits without a bound.

        Raises:
            TimeoutError: The turn didn't come within ``timeout_seconds``.
        """
        span = self._open_span_if_topmost(parent_span)
        if span is not None:
            return span
        async with asyncio.timeout(timeout_seconds):
            while span is None:
                waiter = asyncio.get_running_loop().create_future()
                self._waiters.append(waiter)
                try:
                    await waiter
                finally:
                    self._waiters.remove(waiter)
                span = self._open_span_if_topmost(parent_span)
        return span

    def open_without_waiting(self, span: SavepointSpan | None) -> SavepointSpan | None:
        """``acquire()`` of a query's span when it is the query's turn already - without a coroutine,
        on the path of every statement of a transaction.

        Args:
            span: The span the query runs under - the current context's, of any lock.

        Returns:
            The new span, or None when the query has to wait (``wait_for_query_span()``).
        """
        # get_own_ancestor_span() and _get_open_ancestor() in one walk: a released span's parent is
        # a span of the same lock.
        while span is not None and (span.owner_lock is not self or span.released):
            span = span.parent
        open_spans = self._open_spans
        if (open_spans[-1] if open_spans else None) is not span:
            return None
        new_span = SavepointSpan(self, span)
        open_spans.append(new_span)
        return new_span

    async def wait_for_query_span(self, span: SavepointSpan | None) -> SavepointSpan:
        """Waits for a query's turn and opens its span - bounded by
        ``AMBIENT_QUERY_SAVEPOINT_WAIT_TIMEOUT_SECONDS``.

        Args:
            span: The span the query runs under - the current context's, of any lock.

        Returns:
            The new span.

        Raises:
            TransactionManagementError: The turn didn't come in time.
        """
        try:
            return await self.acquire(
                self.get_own_ancestor_span(span), timeout_seconds=AMBIENT_QUERY_SAVEPOINT_WAIT_TIMEOUT_SECONDS
            )
        except TimeoutError:
            raise TransactionManagementError(
                "Timed out waiting to run a query - a concurrent asyncio.gather()/TaskGroup "
                "sibling on this same transaction has its own nested savepoint (async with "
                "conn._in_transaction():) open and hasn't released or rolled it back within "
                f"{AMBIENT_QUERY_SAVEPOINT_WAIT_TIMEOUT_SECONDS:.0f}s. This usually means that "
                "sibling is awaiting something unrelated to the database (another task, an "
                "external signal) while its savepoint is still open - close the savepoint "
                "before awaiting anything that doesn't itself need it open."
            ) from None

    def release(self, span: SavepointSpan) -> None:
        """Closes ``span`` and wakes every acquire() waiting for its turn, to check it again.

        Args:
            span: a span this lock opened and has not closed yet.
        """
        self._open_spans.remove(span)
        span.released = True
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)

    def _open_span_if_topmost(self, parent_span: SavepointSpan | None) -> SavepointSpan | None:
        """Opens a new span under the nearest still-open ancestor of ``parent_span`` when that
        ancestor is the topmost open span.

        Args:
            parent_span: a span of this lock, or None at the top level.

        Returns:
            The new span, or None when it is not this ancestor's turn yet.
        """
        ancestor = self._get_open_ancestor(parent_span)
        if self._current_top() is not ancestor:
            return None
        span = SavepointSpan(self, ancestor)
        self._open_spans.append(span)
        return span

    def get_own_ancestor_span(self, span: SavepointSpan | None) -> SavepointSpan | None:
        """The nearest span this lock created, walking up from ``span``. A span of another connection's
        lock is no ancestor here - waiting on it would never end.

        Args:
            span: The span to start from - usually the current context's.

        Returns:
            The span, None when no ancestor belongs to this lock.
        """
        while span is not None and span.owner_lock is not self:
            span = span.parent
        return span

    @staticmethod
    def _get_open_ancestor(span: SavepointSpan | None) -> SavepointSpan | None:
        while span is not None and span.released:
            span = span.parent
        return span

    def _current_top(self) -> SavepointSpan | None:
        return self._open_spans[-1] if self._open_spans else None
