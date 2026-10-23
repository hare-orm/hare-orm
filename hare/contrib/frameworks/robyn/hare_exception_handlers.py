"""The HTTP answers to ORM and request query errors - in Robyn's JSON error format."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

import orjson
from robyn import Headers, Request, Response

from hare.contrib.frameworks.error_answers import ErrorAnswers
from hare.contrib.frameworks.robyn.constants import (
    INVALID_PARAMETERS_STATUS,
    JSON_CONTENT_TYPE,
    VALIDATION_ERROR_TITLE,
)
from hare.contrib.request_query.exceptions import InvalidRequestQuery


class HareExceptionHandlers:
    """Answers the errors a route leaves to the application: an ORM or request query error with
    its ``ErrorAnswers`` status, ``{"detail": ...}``; ``InvalidRequestQuery`` -
    ``422 Unprocessable Content`` as Robyn answers an invalid pydantic body itself:
    ``{"error": "Validation Error", "detail": [{"type", "loc": ["query", name], "msg", "input"}]}``.

    Robyn has one exception handler per application: ``HareRobyn`` installs ``handle()``; an
    application answering errors itself calls ``get_response()`` first in its own handler.
    """

    @classmethod
    def get_response(cls, error: Exception) -> Response | None:
        """The answer to an error of the ORM or a request query.

        Args:
            error: The error.

        Returns:
            The answer, None for another error.
        """
        status = ErrorAnswers.get_status(error)
        if status is None:
            return None
        return cls.get_json_response(status, {"detail": ErrorAnswers.get_detail(error, status)})

    @classmethod
    def handle(cls, error: Exception) -> Response:
        """Robyn's exception handler: answers an error of the ORM or a request query, and leaves any
        other to Robyn, which answers it with ``500 Internal Server Error``.

        Args:
            error: The error.

        Returns:
            The answer.

        Raises:
            Exception: ``error`` itself, when it isn't one of the ORM or a request query.
        """
        response = cls.get_response(error)
        if response is None:
            raise error
        return response

    @classmethod
    def get_invalid_request_query_response(cls, request: Request, error: InvalidRequestQuery) -> Response:
        """The answer to request parameters a request query refused.

        Args:
            request: The request.
            error: The error.

        Returns:
            ``422 Unprocessable Content`` with the errors under ``detail``.
        """
        details = [cls.get_validation_error(request, item) for item in error.errors]
        return cls.get_json_response(INVALID_PARAMETERS_STATUS, {"error": VALIDATION_ERROR_TITLE, "detail": details})

    @classmethod
    def get_validation_error(cls, request: Request, error: dict[str, Any]) -> dict[str, Any]:
        """One error of a request query as a validation error.

        Args:
            request: The request.
            error: The error: ``loc``, ``msg``, ``type``.

        Returns:
            The error with the parameter's source first in ``loc`` and the value the request gave
            it as ``input`` - for an error of the whole query, ``loc`` is ``["query"]`` and
            ``input`` the query's parameters.
        """
        parameter_values = (request.query_params.get_all(str(error["loc"][0])) or []) if error["loc"] else []
        return ErrorAnswers.get_validation_error(
            error,
            dict(request.path_params or {}),
            parameter_values,
            cls.get_query_values(request) if not error["loc"] else {},
        )

    @staticmethod
    def get_query_values(request: Request) -> dict[str, Any]:
        """The request's query parameters.

        Args:
            request: The request.

        Returns:
            Each parameter's value, or the list of its values when it repeats.
        """
        return {
            name: values[-1] if len(values) == 1 else values for name, values in request.query_params.to_dict().items()
        }

    @staticmethod
    def get_json_response(status: HTTPStatus, content: Any) -> Response:
        """A JSON answer.

        Args:
            status: The status.
            content: The body.

        Returns:
            The answer.
        """
        return Response(
            status_code=status.value,
            headers=Headers({"Content-Type": JSON_CONTENT_TYPE}),
            description=orjson.dumps(content).decode(),
        )
