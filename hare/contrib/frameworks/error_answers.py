from __future__ import annotations

from http import HTTPStatus

from hare.contrib.frameworks.constants import STATUS_BY_ERROR
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
