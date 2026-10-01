"""The HTTP answers to ORM and request query errors - in Litestar's own error format."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast

from litestar.exceptions import HTTPException, ValidationException
from litestar.exceptions.responses import create_exception_response

from hare.contrib.frameworks.constants import STATUS_BY_ERROR
from hare.contrib.frameworks.error_answers import ErrorAnswers
from hare.contrib.frameworks.litestar.constants import PATH_PARAMETER_SOURCE, QUERY_PARAMETER_SOURCE
from hare.contrib.request_query.exceptions import InvalidRequestQuery, RequestQueryForbidden

if TYPE_CHECKING:  # pragma: nocoverage
    from litestar import Request, Response
    from litestar.types import ExceptionHandlersMap


class HareExceptionHandlers:
    """Answers the errors a handler leaves to the application: an ORM or request query error with
    its ``ErrorAnswers`` status - a forbidden request query parameter named under ``extra``;
    ``InvalidRequestQuery`` - ``400 Bad Request`` as Litestar answers invalid parameters itself:
    each error under ``extra`` as ``{"message", "key", "source"}``.
    """

    @staticmethod
    def answer(request: Request[Any, Any, Any], exception: Exception) -> Response[Any]:
        """Answers an ORM or request query error with its status.

        Args:
            request: The request.
            exception: The error.

        Returns:
            The answer.
        """
        status = cast("HTTPStatus", ErrorAnswers.get_status(exception))
        extra = {"parameter": exception.parameter} if isinstance(exception, RequestQueryForbidden) else None
        return create_exception_response(
            request,
            HTTPException(status_code=status.value, detail=ErrorAnswers.get_detail(exception, status), extra=extra),
        )

    @staticmethod
    def invalid_request_query(request: Request[Any, Any, Any], exception: InvalidRequestQuery) -> Response[Any]:
        """Answers request parameters a request query refused.

        Args:
            request: The request.
            exception: The error.

        Returns:
            ``400 Bad Request`` with the errors under ``extra`` - an error of the whole query has no
            ``key``.
        """
        extra: list[dict[str, Any]] = []
        for error in exception.errors:
            if not error["loc"]:
                extra.append({"message": error["msg"], "source": QUERY_PARAMETER_SOURCE})
                continue
            key = ".".join(str(part) for part in error["loc"])
            source = PATH_PARAMETER_SOURCE if error["loc"][0] in request.path_params else QUERY_PARAMETER_SOURCE
            extra.append({"message": error["msg"], "key": key, "source": source})
        return create_exception_response(
            request,
            ValidationException(detail=f"Validation failed for {request.method} {request.url.path}", extra=extra),
        )

    @classmethod
    def get_handlers(cls) -> ExceptionHandlersMap:
        """The handlers by the error they answer.

        Returns:
            The map for ``Litestar(exception_handlers=...)``.
        """
        return {**dict.fromkeys(STATUS_BY_ERROR, cls.answer), InvalidRequestQuery: cls.invalid_request_query}
