from __future__ import annotations

from typing import Any

from hare.exceptions import QueryError


class TenantValues:
    """The several ``Meta.tenant_field`` values a tenant scope allows - what
    ``Tenancy.any_of("msk", "kzn")`` returns.

    Args:
        values: The values, each once.

    Raises:
        QueryError: No value is given, or one of them is None.
    """

    __slots__ = ("values",)

    def __init__(self, *values: Any) -> None:
        if not values:
            raise QueryError(
                "Tenancy.any_of() needs at least one value - no value neither means every tenant "
                "(Tenancy.ALL) nor no rows"
            )
        if any(value is None for value in values):
            raise QueryError("Tenancy.any_of() takes tenant values - None is not one")
        self.values: tuple[Any, ...] = tuple(dict.fromkeys(values))

    def __eq__(self, other: object) -> bool:
        return type(other) is TenantValues and other.values == self.values

    def __hash__(self) -> int:
        return hash(self.values)

    def __repr__(self) -> str:
        return f"Tenancy.any_of({', '.join(repr(value) for value in self.values)})"

    @staticmethod
    def get_single_value_or_scope(scope: Any) -> Any:
        """A scope of one value as that value - one value is the same scope however it is given.

        Args:
            scope: A tenant value, a ``TenantValues`` or ``Tenancy.ALL``.

        Returns:
            The only value of a ``TenantValues`` holding one, ``scope`` itself otherwise.
        """
        if type(scope) is TenantValues and len(scope.values) == 1:
            return scope.values[0]
        return scope
