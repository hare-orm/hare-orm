"""Hare in Litestar: ``HarePlugin`` opens the Hare context with the application, answers ORM
errors with HTTP responses, runs requests in transactions, builds request queries
(``hare.contrib.request_query``) as handler dependencies and shapes the hare models a handler
returns with ``HareDTO``."""

from hare.contrib.frameworks.litestar.constants import SKIP_TRANSACTION_OPT_KEY
from hare.contrib.frameworks.litestar.dependencies import RequestQueryDIPlugin
from hare.contrib.frameworks.litestar.dto import HareDTO
from hare.contrib.frameworks.litestar.exception_handlers import HareExceptionHandlers
from hare.contrib.frameworks.litestar.middleware.repeated_query_reset_middleware import RepeatedQueryResetMiddleware
from hare.contrib.frameworks.litestar.middleware.transaction_middleware import TransactionMiddleware
from hare.contrib.frameworks.litestar.plugin import HarePlugin

__all__ = (
    "SKIP_TRANSACTION_OPT_KEY",
    "HareDTO",
    "HareExceptionHandlers",
    "HarePlugin",
    "RepeatedQueryResetMiddleware",
    "RequestQueryDIPlugin",
    "TransactionMiddleware",
)
