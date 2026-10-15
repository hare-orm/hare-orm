from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

from hare.exceptions import DoesNotExist, MultipleObjectsReturned
from hare.query.enums import GetException
from hare.query.queryset.options.query_options import QueryOptions

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.query_specification import QuerySpecification
    from hare.query.queryset.single_rows.get_exception_argument import GetExceptionArgument


class GetExceptions:
    """What a single-row query raises or gives when no row or more than one row matches - the standard
    DoesNotExist and MultipleObjectsReturned, the exceptions get() was given, or None."""

    @staticmethod
    def check_exception_argument(name: str, value: Any) -> None:
        """Checks an exception parameter of ``get()``.

        Args:
            name: The parameter's name.
            value: Its value.

        Raises:
            TypeError: The value is neither ``GetException.STANDARD``, None, an exception class nor
                an exception instance.
        """
        if value is None or value is GetException.STANDARD or isinstance(value, BaseException):
            return
        if isinstance(value, type) and issubclass(value, BaseException):
            return
        raise TypeError(f"get(): {name} must be an exception class, an exception instance or None, got {value!r}")

    @staticmethod
    def raise_object_does_not_exist(specification: QuerySpecification[Any]) -> NoReturn:
        """Raises for a query that matched no row: ``DoesNotExist``, or the exception ``.get(...,
        does_not_exist_exception=...)`` named - a class is instantiated with no arguments, an
        instance is raised as is.

        Args:
            specification: The query specification - a queryset or a query made from one.
        """
        if specification._does_not_exist_exception is not None:
            override = specification._does_not_exist_exception
            raise override if isinstance(override, BaseException) else override()
        raise DoesNotExist(specification.model)

    @staticmethod
    def set_get_exceptions(
        specification: QuerySpecification[Any],
        does_not_exist_exception: GetExceptionArgument,
        multiple_objects_returned_exception: GetExceptionArgument,
    ) -> None:
        """Keeps the exceptions a ``get()`` raises instead of the standard ones.

        Args:
            specification: The query specification - a queryset or a query made from one.
            does_not_exist_exception: As ``get()`` takes it.
            multiple_objects_returned_exception: As ``get()`` takes it.
        """
        does_not_exist_override = (
            None if does_not_exist_exception is GetException.STANDARD else does_not_exist_exception
        )
        multiple_objects_returned_override = (
            None
            if multiple_objects_returned_exception is GetException.STANDARD
            else multiple_objects_returned_exception
        )
        if (
            does_not_exist_override is not None
            or multiple_objects_returned_override is not None
            or specification._options is not QueryOptions.DEFAULT
        ):
            specification._options = specification._options.updated(
                does_not_exist_exception=does_not_exist_override,
                multiple_objects_returned_exception=multiple_objects_returned_override,
            )

    @staticmethod
    def raise_multiple_objects_returned(specification: QuerySpecification[Any]) -> NoReturn:
        """Raises for a single-row query that matched more than one row: ``MultipleObjectsReturned``,
        or the exception ``.get(..., multiple_objects_returned_exception=...)`` named - a class is
        instantiated with no arguments, an instance is raised as is.

        Args:
            specification: The query specification - a queryset or a query made from one.
        """
        if specification._multiple_objects_returned_exception is not None:
            override = specification._multiple_objects_returned_exception
            raise override if isinstance(override, BaseException) else override()
        raise MultipleObjectsReturned(specification.model)

    @staticmethod
    def empty_single_or_list_result(specification: QuerySpecification[Any]) -> Any:
        """The result of a rows query that matched nothing - what ``.none()`` returns without a query.

        Args:
            specification: The query specification - a queryset or a query made from one.

        Raises:
            DoesNotExist: The query is for a single row that must exist.
        """
        if specification._single:
            if specification._raise_does_not_exist:
                GetExceptions.raise_object_does_not_exist(specification)
            return None
        return []
