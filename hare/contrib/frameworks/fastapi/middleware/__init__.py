"""The ASGI middleware ``HareFastAPI`` adds: a request's own count of repeated queries and, when
``atomic_requests`` is on, a transaction around each request."""

from hare.contrib.frameworks.fastapi.middleware.repeated_query_reset_middleware import (
    REQUEST_SCOPE_TYPES,
    RepeatedQueryResetMiddleware,
)
from hare.contrib.frameworks.fastapi.middleware.transaction_failing_handler import TransactionFailingHandler
from hare.contrib.frameworks.fastapi.middleware.transaction_middleware import TransactionMiddleware

__all__ = [
    "REQUEST_SCOPE_TYPES",
    "RepeatedQueryResetMiddleware",
    "TransactionMiddleware",
    "TransactionFailingHandler",
]
