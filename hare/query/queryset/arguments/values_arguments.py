from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import QueryError
from hare.query.expressions import Expression
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.queryset import QuerySet


class ValuesArguments:
    """The checks of what values() and values_list() are given - field names or expressions of the
    right types, on a queryset not loading model fields or relations the rows don't use."""

    @staticmethod
    def raise_if_values_unusable(
        queryset: QuerySet[Any, Any],
        method_name: str,
        field_names: tuple[Any, ...],
        expressions: dict[str, Any],
        *,
        allow_field_name_kwargs: bool,
    ) -> None:
        """Rejects a ``.values()``/``.values_list()`` call with arguments of the wrong type, or on a
        queryset choosing model fields or relations to load.

        Args:
            queryset: The queryset.
            method_name: The calling method, for the error message.
            field_names: The positional arguments.
            expressions: The keyword arguments.
            allow_field_name_kwargs: Whether a keyword argument may be a field name string.

        Raises:
            QueryError: An argument has the wrong type, or the queryset uses ``.only()``,
                ``.defer()``, ``prefetch_related()`` or a ``select_related()`` relation the rows
                don't use.
        """
        ValuesArguments.raise_if_invalid_values_arguments(
            method_name, field_names, expressions, allow_field_name_kwargs=allow_field_name_kwargs
        )
        if queryset._fields_for_select:
            raise QueryError(f"{method_name} cannot be used with .only()")
        if queryset._deferred_fields:
            raise QueryError(f"{method_name} cannot be used with .defer()")
        result_type = "dicts" if method_name == ".values()" else "tuples"
        if queryset._prefetch_map or queryset._prefetch_queries:
            raise QueryError(
                f"{method_name} cannot be used with prefetch_related() - the result is plain {result_type}, "
                "not model instances, so there's nothing to attach a prefetched relation to."
            )
        ValuesArguments.raise_if_select_related_unused_by_values(queryset, method_name, result_type)

    @staticmethod
    def raise_if_invalid_values_arguments(
        method_name: str, field_names: tuple[Any, ...], expressions: dict[str, Any], *, allow_field_name_kwargs: bool
    ) -> None:
        """Rejects a ``.values()``/``.values_list()`` argument that is neither a field name nor an
        expression.

        Args:
            method_name: The calling method, for the error message.
            field_names: The positional arguments.
            expressions: The keyword arguments.
            allow_field_name_kwargs: Whether a keyword argument may be a field name string.

        Raises:
            QueryError: An argument has the wrong type.
        """
        for field_name in field_names:
            if not isinstance(field_name, str):
                raise QueryError(f"{method_name} positional arguments must be field names, got {field_name!r}")
        for key, value in expressions.items():
            if isinstance(value, (Expression, Term)) or (allow_field_name_kwargs and isinstance(value, str)):
                continue
            expected = "a field name or an expression" if allow_field_name_kwargs else "an expression"
            raise QueryError(
                f"{method_name} keyword argument {key!r} must be {expected} (F(), Value(), a function, ...), "
                f"got {value!r}"
            )

    @staticmethod
    def raise_if_select_related_unused_by_values(
        queryset: QuerySet[Any, Any], method_name: str, result_type: str
    ) -> None:
        """Rejects an explicit ``select_related()`` relation that ``.values()``/``.values_list()``
        would ignore - one carrying no ``Select(..., extra_condition=...)`` and not the parent path
        of one.

        Args:
            queryset: The queryset.
            method_name: The calling method, for the message.
            result_type: What the method returns ("dicts" / "tuples").

        Raises:
            ValueError: A relation has no effect on the result.
        """
        extra_condition_paths = queryset._select_related_extra_conditions.keys()
        for relation_path in sorted(queryset._explicitly_select_related):
            if relation_path in extra_condition_paths or any(
                extra_condition_path.startswith(f"{relation_path}__") for extra_condition_path in extra_condition_paths
            ):
                continue
            raise QueryError(
                f"{method_name} cannot be used with select_related({relation_path!r}) - the result is "
                f"plain {result_type}, not model instances, so there's nothing to attach a joined "
                f'relation to. Select the relation\'s own fields directly (e.g. "{relation_path}__name") '
                "or pass Select(relation, extra_condition=...) to condition that relation's JOIN."
            )

    @staticmethod
    def get_every_selected_name(queryset: QuerySet[Any, Any]) -> list[str]:
        """The names ``.values()``/``.values_list()`` without arguments select - every stored field
        of the model, then every annotation that isn't an ``.alias()``.

        Args:
            queryset: The queryset.

        Returns:
            The names.
        """
        return [
            field for field in queryset.model._meta.fields_map if field in queryset.model._meta.fields_db_projection
        ] + [key for key in queryset._annotations if key not in queryset._alias_keys]

    @staticmethod
    def raise_if_values_selected(queryset: QuerySet[Any, Any], method_name: str) -> None:
        """Rejects a method reading or returning model instances on a queryset returning the
        values ``.values()``/``.values_list()`` select.

        Args:
            queryset: The queryset.
            method_name: The method.

        Raises:
            QueryError: The queryset selects values.
        """
        if queryset._selection is not None:
            raise QueryError(
                f"Cannot call {method_name}() after .values() or .values_list() - call it on the queryset "
                "before .values()/.values_list() instead."
            )
