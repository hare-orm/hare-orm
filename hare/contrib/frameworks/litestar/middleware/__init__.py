"""The middleware ``HarePlugin`` adds: a request's own count of repeated queries and, when
``atomic_requests`` is on, a transaction around each request."""

from hare.contrib.frameworks.litestar.middleware.repeated_query_reset_middleware import RepeatedQueryResetMiddleware
from hare.contrib.frameworks.litestar.middleware.transaction_middleware import TransactionMiddleware

__all__ = [
    "RepeatedQueryResetMiddleware",
    "TransactionMiddleware",
]
