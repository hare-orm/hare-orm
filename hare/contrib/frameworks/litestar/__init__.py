"""Hare in Litestar: ``HarePlugin`` opens the Hare context with the application, answers ORM
errors with HTTP responses, runs requests in transactions, builds request queries
(``hare.contrib.request_query``) as handler dependencies and shapes the hare models a handler
returns with ``HareDTO``."""

from __future__ import annotations

from hare.contrib.frameworks.litestar.constants import SKIP_TRANSACTION_OPT_KEY
from hare.contrib.frameworks.litestar.hare_dto import HareDTO
from hare.contrib.frameworks.litestar.hare_exception_handlers import HareExceptionHandlers
from hare.contrib.frameworks.litestar.hare_plugin import HarePlugin
from hare.contrib.frameworks.litestar.middleware.request_state_reset_middleware import RequestStateResetMiddleware
from hare.contrib.frameworks.litestar.middleware.transaction_middleware import TransactionMiddleware
from hare.contrib.frameworks.litestar.request_query_di_plugin import RequestQueryDIPlugin

__all__ = (
    "SKIP_TRANSACTION_OPT_KEY",
    "HareDTO",
    "HareExceptionHandlers",
    "HarePlugin",
    "RequestStateResetMiddleware",
    "RequestQueryDIPlugin",
    "TransactionMiddleware",
)
