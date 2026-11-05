from __future__ import annotations

from typing import ClassVar

from hare.sql.terms.containers.container_function import ContainerFunction


class ArrayContainsTerm(ContainerFunction):
    """``array`` holds every element of ``other`` - ``(array, other)``."""

    structure_attributes = ()

    function_name: ClassVar[str] = "array_contains"


class ArrayContainedByTerm(ContainerFunction):
    """Every element of ``array`` is in ``other`` - ``(array, other)``."""

    structure_attributes = ()

    function_name: ClassVar[str] = "array_contained_by"


class ArrayOverlapTerm(ContainerFunction):
    """``array`` and ``other`` share an element - ``(array, other)``."""

    structure_attributes = ()

    function_name: ClassVar[str] = "array_overlap"


class ArrayLengthTerm(ContainerFunction):
    """The number of elements of ``array`` - 0 for an empty one, NULL for NULL - ``(array,)``."""

    structure_attributes = ()

    function_name: ClassVar[str] = "array_length"


class MapContainsKeyTerm(ContainerFunction):
    """``map`` has the key ``key`` - ``(map, key)``."""

    structure_attributes = ()

    function_name: ClassVar[str] = "map_contains_key"


class MapKeysTerm(ContainerFunction):
    """The keys of ``map`` as an array - ``(map,)``."""

    structure_attributes = ()

    function_name: ClassVar[str] = "map_keys"


class MapValuesTerm(ContainerFunction):
    """The values of ``map`` as an array - ``(map,)``."""

    structure_attributes = ()

    function_name: ClassVar[str] = "map_values"
