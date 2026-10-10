from __future__ import annotations

from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Any

from hare.contrib.frameworks.constants import PATH_PARAMETER_SOURCE, QUERY_PARAMETER_SOURCE, STATUS_BY_ERROR
from hare.contrib.request_query.exceptions import RequestQueryForbidden


class ErrorAnswers:
    """What every framework integration answers an ORM or request query error with - each in its
    own response format:

    - ``DoesNotExist`` - ``404 Not Found``;
    - ``IntegrityError`` (a unique or foreign key constraint, a protected relation, a stale
      optimistic lock) - ``409 Conflict``, without the database's message, which names tables and
      constraints;
    - ``RequestQueryForbidden`` (a parameter asks for what the request may not see) -
      ``403 Forbidden`` with the reason.
    """

    @staticmethod
    def get_status(error: Exception) -> HTTPStatus | None:
        """The status of an error.

        Args:
            error: The error.

        Returns:
            The status, None for an error the integrations leave to the framework.
        """
        for error_type, status in STATUS_BY_ERROR.items():
            if isinstance(error, error_type):
                return status
        return None

    @staticmethod
    def get_detail(error: Exception, status: HTTPStatus) -> str:
        """The message of the answer.

        Args:
            error: The error.
            status: Its status.

        Returns:
            The reason of a forbidden request query parameter, the status's phrase otherwise.
        """
        if isinstance(error, RequestQueryForbidden):
            return str(error)
        return status.phrase

    @staticmethod
    def get_validation_error(
        error: dict[str, Any],
        path_parameters: Mapping[str, Any],
        parameter_values: Sequence[Any],
        query_values: Mapping[str, Any],
    ) -> dict[str, Any]:
        """One error of a request query as a validation error.

        Args:
            error: The error: ``loc``, ``msg``, ``type``.
            path_parameters: The request's path parameters.
            parameter_values: The values the request's query gave the error's parameter.
            query_values: The request's query parameters - the input of an error of the whole query.

        Returns:
            The error with the parameter's source first in ``loc`` and the value the request gave
            it as ``input`` - for an error of the whole query, ``loc`` is ``["query"]`` and
            ``input`` the query's parameters.
        """
        if not error["loc"]:
            return {
                "type": error["type"],
                "loc": [QUERY_PARAMETER_SOURCE],
                "msg": error["msg"],
                "input": query_values,
            }
        parameter = str(error["loc"][0])
        given: Any
        if parameter in path_parameters:
            source, given = PATH_PARAMETER_SOURCE, path_parameters[parameter]
        else:
            source = QUERY_PARAMETER_SOURCE
            given = parameter_values[-1] if len(parameter_values) == 1 else parameter_values or None
        return {"type": error["type"], "loc": [source, *error["loc"]], "msg": error["msg"], "input": given}
