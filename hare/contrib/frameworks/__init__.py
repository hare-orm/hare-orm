"""Hare in web frameworks - one subpackage per framework (``litestar``, ``fastapi``, ``robyn``):
it opens the Hare context with the application, answers ORM errors with HTTP responses, runs
requests in transactions and builds request queries (``hare.contrib.request_query``) from a
request's parameters. What doesn't depend on the framework is here: the application's lifecycle,
the request's transaction, the page schemas."""

from hare.contrib.frameworks.error_answers import ErrorAnswers
from hare.contrib.frameworks.exceptions import ServerErrorResponse
from hare.contrib.frameworks.lifecycle import HareLifecycle
from hare.contrib.frameworks.pages.cursor_page_schema import CursorPageSchema
from hare.contrib.frameworks.pages.page_schema import PageSchema
from hare.contrib.frameworks.pages.scroll_page_schema import ScrollPageSchema
from hare.contrib.frameworks.parameters import ParameterAnnotation
from hare.contrib.frameworks.transactions import RequestTransaction

__all__ = (
    "CursorPageSchema",
    "ErrorAnswers",
    "HareLifecycle",
    "PageSchema",
    "ParameterAnnotation",
    "RequestTransaction",
    "ScrollPageSchema",
    "ServerErrorResponse",
)
