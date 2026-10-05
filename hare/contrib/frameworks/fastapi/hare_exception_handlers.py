"""The HTTP answers to ORM and request query errors - in FastAPI's own error format."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError

from hare.contrib.frameworks.constants import STATUS_BY_ERROR
from hare.contrib.frameworks.error_answers import ErrorAnswers
from hare.contrib.request_query.exceptions import InvalidRequestQuery

if TYPE_CHECKING:  # pragma: nocoverage
    from collections.abc import Awaitable, Callable

    from fastapi import Request
    from starlette.responses import Response


class HareExceptionHandlers:
    """Answers the errors a route leaves to the application, as FastAPI answers its own: an ORM
    or request query error with its ``ErrorAnswers`` status, ``{"detail": ...}``;
    ``InvalidRequestQuery`` - ``422 Unprocessable Content`` as FastAPI's own invalid parameters:
    ``{"detail": [{"type", "loc": ["query", name], "msg", "input"}]}``.
    """

    @staticmethod
    async def answer(request: Request, exception: Exception) -> Response:
        """Answers an ORM or request query error with its status.

        Args:
            request: The request.
            exception: The error.

        Returns:
            The answer.
        """
        status = cast("HTTPStatus", ErrorAnswers.get_status(exception))
        return await http_exception_handler(
            request, HTTPException(status.value, detail=ErrorAnswers.get_detail(exception, status))
        )

    @classmethod
    async def invalid_request_query(cls, request: Request, exception: Exception) -> Response:
        """Answers request parameters a request query refused.

        Args:
            request: The request.
            exception: The ``InvalidRequestQuery``.

        Returns:
            ``422 Unprocessable Content`` with the errors under ``detail``.
        """
        errors = exception.errors if isinstance(exception, InvalidRequestQuery) else []
        return await request_validation_exception_handler(
            request, RequestValidationError([cls.get_validation_error(request, error) for error in errors])
        )

    @classmethod
    def get_validation_error(cls, request: Request, error: dict[str, Any]) -> dict[str, Any]:
        """One error of a request query as FastAPI writes a validation error.

        Args:
            request: The request.
            error: The error: ``loc``, ``msg``, ``type``.

        Returns:
            The error with the parameter's source first in ``loc`` and the value the request gave
            it as ``input`` - for an error of the whole query, ``loc`` is ``["query"]`` and
            ``input`` the query's parameters.
        """
        parameter_values = request.query_params.getlist(str(error["loc"][0])) if error["loc"] else []
        return ErrorAnswers.get_validation_error(
            error, request.path_params, parameter_values, cls.get_query_values(request) if not error["loc"] else {}
        )

    @staticmethod
    def get_query_values(request: Request) -> dict[str, Any]:
        """The request's query parameters.

        Args:
            request: The request.

        Returns:
            Each parameter's value, or the list of its values when it repeats.
        """
        query_values: dict[str, Any] = {}
        for name in request.query_params:
            values = request.query_params.getlist(name)
            query_values[name] = values[-1] if len(values) == 1 else values
        return query_values

    @classmethod
    def get_handlers(cls) -> dict[type[Exception], Callable[[Request, Exception], Awaitable[Response]]]:
        """The handlers by the error they answer.

        Returns:
            The map for ``FastAPI(exception_handlers=...)``.
        """
        return {**dict.fromkeys(STATUS_BY_ERROR, cls.answer), InvalidRequestQuery: cls.invalid_request_query}
