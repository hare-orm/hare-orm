from __future__ import annotations

from hare.core.routing.primary_reads import PrimaryReads
from hare.core.routing.written_connections import WrittenConnections


class Routing:
    """Where the reads of a routed model go besides what the routers say: after a write in the same
    task, to the connection written through (``read_your_writes_seconds`` of the config bounds how
    long); inside ``using_primary()``, always there."""

    @staticmethod
    def using_primary() -> PrimaryReads:
        """A block whose every read goes to the connection its model is written through::

            async with Routing.using_primary():
                order = await Order.objects.get(pk=order_id)

        Returns:
            The block - ``with`` or ``async with``.
        """
        return PrimaryReads()

    @staticmethod
    def forget_writes() -> None:
        """Lets the current task read from the replicas again, its writes forgotten - for a task
        that serves several requests one after another."""
        WrittenConnections.current.set(None)
