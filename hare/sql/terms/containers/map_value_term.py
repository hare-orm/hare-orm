from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.sql.terms.containers.container_function import ContainerFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field


class MapValueTerm(ContainerFunction):
    """The value of a map under a key - the value type's default for a missing key.

    Args:
        map_term: The map.
        key: The key, bound as the map's key field writes it.
        value_field: The field of the map's values.
        alias: The alias.
    """

    function_name: ClassVar[str] = "map_value"

    def __init__(self, map_term: Any, key: Any, value_field: Field[Any], alias: str | None = None) -> None:
        super().__init__(map_term, key, alias=alias)
        self.value_field = value_field
