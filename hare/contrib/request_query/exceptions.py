from __future__ import annotations

from typing import Any

from hare.exceptions import HareError


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

    def __reduce__(self) -> tuple[type[InvalidRequestQuery], tuple[Any, ...]]:
        return self.__class__, (self.errors,)

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

    def __reduce__(self) -> tuple[type[RequestQueryForbidden], tuple[Any, ...]]:
        return self.__class__, (self.parameter, self.args[0])


class ApiGetterError(HareError):
    """A value from another service couldn't be loaded (``Meta.api_getter``). A framework adapter answers it
    with ``502 Bad Gateway``.

    Args:
        message: What failed.
        source_name: The source that failed, None for a target left empty.
    """

    def __init__(self, message: str, source_name: str | None = None) -> None:
        self.source_name = source_name
        super().__init__(message)

    def __reduce__(self) -> tuple[type[ApiGetterError], tuple[Any, ...]]:
        return self.__class__, (self.args[0], self.source_name)


class ApiDataNotFoundError(ApiGetterError):
    """A required source answered ``404 Not Found``, or had no item for a value asked."""


class ApiRequestError(ApiGetterError):
    """A required source couldn't be reached, or answered an error."""


class ApiValueMissingError(ApiGetterError):
    """A required source had no value of its key to ask for, or a required target stayed empty."""


class ApiValueConversionError(ApiGetterError):
    """A mapping's ``field`` or ``convert`` refused a value."""
