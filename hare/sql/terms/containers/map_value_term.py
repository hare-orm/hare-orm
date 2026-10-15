from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.sql.terms.containers.container_function import ContainerFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field


class MapValueTerm(ContainerFunction):
    """The value of a map under a key - the value type's default for a missing key.

    Args:
        map_term: The map.
        key: The key's Python value - a dialect's renderer binds it as its types write the key field.
        key_field: The field of the map's keys.
        value_field: The field of the map's values.
        alias: The alias.
    """

    function_name: ClassVar[str] = "map_value"

    def __init__(
        self, map_term: Any, key: Any, key_field: Field[Any], value_field: Field[Any], alias: str | None = None
    ) -> None:
        super().__init__(map_term, alias=alias)
        self.key = key
        self.key_field = key_field
        self.value_field = value_field
