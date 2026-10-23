from __future__ import annotations

from typing import Any, ClassVar

from hare.sql.terms.containers.array_element_term import ArrayElementTerm
from hare.sql.terms.containers.container_function import ContainerFunction


class ArraySliceTerm(ContainerFunction):
    """The elements of an array from the 0-based ``start`` up to, not including, ``end`` - as Python
    slices a list.

    Args:
        array: The array.
        start: The first element.
        end: The element after the last one.
        alias: The alias.

    Raises:
        ValidationError: A bound isn't an int in range.
    """

    function_name: ClassVar[str] = "array_slice"

    def __init__(self, array: Any, start: int, end: int, alias: str | None = None) -> None:
        super().__init__(array, alias=alias)
        self.start = ArrayElementTerm.get_validated_index(start)
        self.end = ArrayElementTerm.get_validated_index(end)
