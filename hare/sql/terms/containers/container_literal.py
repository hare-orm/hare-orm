from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.sql.terms.containers.container_function import ContainerFunction
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field


class ContainerLiteral(ContainerFunction):
    """A container value compared with a container column - typed as the column, where a literal's
    own type would be the narrowest its elements fit. The value is a node of the tree (a ``ValueWrapper``
    argument), so a query plan binds another value in its place.

    Args:
        value: The value as the dialect binds it (its field's ``get_db_value()``).
        field: The container field the value is of.
        alias: The alias.
    """

    function_name: ClassVar[str] = "container_literal"

    def __init__(self, value: Any, field: Field[Any], alias: str | None = None) -> None:
        super().__init__(ValueWrapper(value), alias=alias)
        self.field = field

    @property
    def value_wrapper(self) -> ValueWrapper:
        """The node holding the value."""
        return self.args[0]  # type: ignore[return-value]

    @property
    def value(self) -> Any:
        """The value as the dialect binds it."""
        return self.value_wrapper.value
