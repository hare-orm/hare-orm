from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.query.queryset.extensions.enums import ExtensionPhase

if TYPE_CHECKING:
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
            limit per group) - ``count()``, ``exists()`` and ``aggregate()`` then read the rows returned.
        transform_result: Shapes the materialized result after ordinary execution. It receives
            QuerySetExtensionResult and the recorded arguments. At most one handler may be used.
        phase: Apply to the row source before wrapping, or to the final statement.
        refuses_writes: Whether a write (``update()``, ``delete()``) refuses the call - it changes how
            rows are read, and a write matches its rows by its conditions alone.
    """

    apply: Callable[..., QueryBuilder]
    reads_query: bool = False
    takes_condition: bool = False
    changes_rows: bool = False
    transform_result: Callable[..., Any] | None = None
    refuses_writes: bool = False
    phase: ExtensionPhase = ExtensionPhase.ROW_SOURCE

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
