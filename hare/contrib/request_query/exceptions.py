from __future__ import annotations

from typing import Any


class InvalidRequestQuery(ValueError):
    """The values of a request don't make a valid request query - a value of the wrong type, an
    ordering the query doesn't allow, a malformed cursor. A framework adapter answers it with
    ``400 Bad Request``.

    Args:
        errors: One dict per error: ``loc`` (the parameter, empty for an error of the whole query -
            a model validator's), ``msg`` and ``type``.
    """

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        super().__init__("; ".join(self.describe_error(error) for error in errors))

    @staticmethod
    def describe_error(error: dict[str, Any]) -> str:
        """One error as text.

        Args:
            error: The error.

        Returns:
            ``<parameter>: <message>``, or the message alone for an error of the whole query.
        """
        if not error["loc"]:
            return str(error["msg"])
        return f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"


class RequestQueryForbidden(PermissionError):
    """A request asks for rows its query doesn't let it see - deleted rows without the right to them.
    A framework adapter answers it with ``403 Forbidden``.

    Args:
        parameter: The parameter asking for them.
        message: Why the request may not see them.
    """

    def __init__(self, parameter: str, message: str) -> None:
        self.parameter = parameter
        super().__init__(message)
