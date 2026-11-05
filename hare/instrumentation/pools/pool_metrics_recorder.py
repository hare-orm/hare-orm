from __future__ import annotations

import threading
import weakref
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

from hare.core.log import logger
from hare.instrumentation.pools.declarations import PoolMeasurement
from hare.instrumentation.pools.enums import PoolMetricEvent
from hare.instrumentation.pools.messages import POOL_RECORDER_ERROR_MESSAGE

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class PoolMetricsRecorder:
    """Dispatch completed events directly, independently of gauge exports and sampling buffers.

    Subscribers belong to instrumentation instances. Clients are held weakly, including those
    that have not connected yet, so instrumentation can start before or after client creation.
    Native statistics call the same callback once per completed measurement, outside counter
    locks. No Python callback is installed while there are no subscribers.
    """

    lock: ClassVar[threading.RLock] = threading.RLock()
    clients: ClassVar[weakref.WeakSet[DatabaseClient]] = weakref.WeakSet()
    subscribers: ClassVar[tuple[Callable[[PoolMeasurement], None], ...]] = ()

    @classmethod
    def attach(cls, client: DatabaseClient) -> None:
        """Associate newly constructed statistics with the client's current labels."""
        with cls.lock:
            cls.clients.add(client)
            cls.configure(client)

    @classmethod
    def configure(cls, client: DatabaseClient) -> None:
        """Install only a weak callback, avoiding a native-statistics/client reference cycle."""
        if not cls.subscribers:
            client.pool_statistics.set_recorder(None)
            return
        reference = weakref.ref(client)

        def record(event: str, value: float) -> None:
            target = reference()
            if target is not None:
                cls.record(
                    PoolMeasurement(
                        target.connection_alias,
                        target.pool_role.value,
                        target.tenant_schema,
                        target.dialect.otel_system_name,
                        PoolMetricEvent(event),
                        value,
                    )
                )

        client.pool_statistics.set_recorder(record)

    @classmethod
    def subscribe(cls, recorder: Callable[[PoolMeasurement], None]) -> None:
        """Subscribe once; repeated starts do not duplicate events."""
        with cls.lock:
            if recorder in cls.subscribers:
                return
            first = not cls.subscribers
            cls.subscribers = (*cls.subscribers, recorder)
            if first:
                for client in tuple(cls.clients):
                    cls.configure(client)

    @classmethod
    def unsubscribe(cls, recorder: Callable[[PoolMeasurement], None]) -> None:
        """Stop future dispatches without retaining closed clients or consuming another reader's data."""
        with cls.lock:
            cls.subscribers = tuple(item for item in cls.subscribers if item != recorder)
            if not cls.subscribers:
                for client in tuple(cls.clients):
                    cls.configure(client)

    @classmethod
    def record(cls, measurement: PoolMeasurement) -> None:
        """Emit on the completion thread; an instrumentation error cannot fail a database operation."""
        with cls.lock:
            subscribers = cls.subscribers
        for subscriber in subscribers:
            try:
                subscriber(measurement)
            except Exception:
                logger.exception(POOL_RECORDER_ERROR_MESSAGE)
