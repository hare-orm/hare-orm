"""The ASGI middleware ``HareFastAPI`` adds: a request's own state (its count of repeated queries,
its writes) and, when
``atomic_requests`` is on, a transaction around each request."""

from __future__ import annotations

from hare.contrib.frameworks.fastapi.constants import REQUEST_SCOPE_TYPES
from hare.contrib.frameworks.fastapi.middleware.request_state_reset_middleware import RequestStateResetMiddleware
from hare.contrib.frameworks.fastapi.middleware.transaction_failing_handler import TransactionFailingHandler
from hare.contrib.frameworks.fastapi.middleware.transaction_middleware import TransactionMiddleware

__all__ = [
    "REQUEST_SCOPE_TYPES",
    "RequestStateResetMiddleware",
    "TransactionMiddleware",
    "TransactionFailingHandler",
]
