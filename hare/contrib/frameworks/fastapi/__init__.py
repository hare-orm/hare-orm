"""Hare in FastAPI: ``HareFastAPI`` opens the Hare context with the application, answers ORM errors
with HTTP responses and runs requests in transactions; ``RequestQueryDependency`` builds request
queries (``hare.contrib.request_query``) as route dependencies. A route returns hare models with
its ``response_model`` - a page of rows with ``PageSchema[RowSchema]``."""

from __future__ import annotations

from hare.contrib.frameworks.fastapi.hare_exception_handlers import HareExceptionHandlers
from hare.contrib.frameworks.fastapi.hare_fast_api import HareFastAPI
from hare.contrib.frameworks.fastapi.middleware.request_state_reset_middleware import RequestStateResetMiddleware
from hare.contrib.frameworks.fastapi.middleware.transaction_middleware import TransactionMiddleware
from hare.contrib.frameworks.fastapi.request_query_dependency import RequestQueryDependency

__all__ = (
    "HareExceptionHandlers",
    "HareFastAPI",
    "RequestStateResetMiddleware",
    "RequestQueryDependency",
    "TransactionMiddleware",
)
