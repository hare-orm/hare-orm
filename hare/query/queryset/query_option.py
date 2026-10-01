from __future__ import annotations

from operator import attrgetter
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast, overload

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.query_spec import QuerySpec

OptionValue = TypeVar("OptionValue")


class QueryOption(Generic[OptionValue]):
    """A query attribute kept in the query's ``QueryOptions``: reading it reads the shared object,
    assigning it gives the query a new object with the value replaced and made immutable. A
    ``property`` over ``operator.attrgetter``, so a read runs without a Python function call.
    """

    def __new__(cls, name: str) -> QueryOption[OptionValue]:
        """
        Args:
            name: The setting in ``QueryOptions``.

        Returns:
            The property, typed as this descriptor.
        """

        def set_option(instance: QuerySpec[Any], value: Any) -> None:
            instance._options = instance._options.updated(**{name: value})

        return cast("QueryOption[OptionValue]", property(attrgetter(f"_options.{name}"), set_option))

    if TYPE_CHECKING:

        @overload
        def __get__(self, instance: None, owner: type[Any]) -> QueryOption[OptionValue]: ...

        @overload
        def __get__(self, instance: QuerySpec[Any], owner: type[Any]) -> OptionValue: ...

        def __get__(self, instance: QuerySpec[Any] | None, owner: type[Any]) -> Any: ...

        def __set__(self, instance: QuerySpec[Any], value: Any) -> None: ...
