"""Hare in FastAPI: ``HareFastAPI`` opens the Hare context with the application, answers ORM errors
with HTTP responses and runs requests in transactions; ``RequestQueryDependency`` builds request
queries (``hare.contrib.request_query``) as route dependencies. A route returns hare models with
its ``response_model`` - a page of rows with ``PageSchema[RowSchema]``."""

from hare.contrib.frameworks.fastapi.application import HareFastAPI
from hare.contrib.frameworks.fastapi.dependencies import RequestQueryDependency
from hare.contrib.frameworks.fastapi.exception_handlers import HareExceptionHandlers
from hare.contrib.frameworks.fastapi.middleware.repeated_query_reset_middleware import RepeatedQueryResetMiddleware
from hare.contrib.frameworks.fastapi.middleware.transaction_middleware import TransactionMiddleware

__all__ = (
    "HareExceptionHandlers",
    "HareFastAPI",
    "RepeatedQueryResetMiddleware",
    "RequestQueryDependency",
    "TransactionMiddleware",
)
