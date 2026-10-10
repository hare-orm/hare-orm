from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.extensions.query_set_extension_query import QuerySetExtensionQuery
    from hare.sql.builder.queries.query_builder import QueryBuilder


@dataclass(frozen=True, slots=True)
class QuerySetExtension:
    """One dialect's implementation of a QuerySet method it registered.

    Attributes:
        apply: Applies a call to the query just built - ``apply(builder, *args, **kwargs)``, or
            ``apply(builder, extension_query, *args, **kwargs)`` when ``reads_query`` - and returns
            the builder.
        reads_query: Whether ``apply`` takes the ``QuerySetExtensionQuery`` of the query too - its
            model and connection, its conditions and fields resolved as its own filters are.
        takes_condition: Whether the method's arguments are one condition, as ``filter()``'s are -
            ``Q`` objects and filters, recorded as one ``Q`` that ``apply`` gets.
        changes_rows: Whether a call changes which rows the query returns beyond its conditions (a
            limit per group) - ``count()`` and ``exists()`` then count the rows returned, and a write
            refuses the call.
        read_result: Reads the result of a query with the call -
            ``await read_result(extension_query, result, *args, **kwargs)`` returns what the query
            returns. None returns the result as read.
    """

    apply: Callable[..., QueryBuilder]
    reads_query: bool = False
    takes_condition: bool = False
    changes_rows: bool = False
    read_result: Callable[..., Awaitable[Any]] | None = None

    def get_call_signature(self) -> inspect.Signature:
        """The parameters a call of the method takes - ``apply``'s after the builder and the query;
        a method taking a condition takes ``filter()``'s.

        Returns:
            The signature, its annotations dropped.
        """
        if self.takes_condition:
            return inspect.Signature(
                [
                    inspect.Parameter("conditions", inspect.Parameter.VAR_POSITIONAL),
                    inspect.Parameter("filters", inspect.Parameter.VAR_KEYWORD),
                ]
            )
        parameters = list(inspect.signature(self.apply).parameters.values())[2 if self.reads_query else 1 :]
        return inspect.Signature([parameter.replace(annotation=inspect.Parameter.empty) for parameter in parameters])

    def apply_call(
        self,
        builder: QueryBuilder,
        extension_query: QuerySetExtensionQuery,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> QueryBuilder:
        """Applies one call to the query just built.

        Args:
            builder: The query's builder.
            extension_query: The query.
            args: The call's positional arguments.
            kwargs: The call's keyword arguments.

        Returns:
            The builder with the call applied.
        """
        if self.reads_query:
            return self.apply(builder, extension_query, *args, **kwargs)
        return self.apply(builder, *args, **kwargs)
