from __future__ import annotations

from typing import Any, ClassVar

from hare.sql.terms.containers.container_function import ContainerFunction


class TupleElementTerm(ContainerFunction):
    """One element of a tuple by its 0-based position.

    Args:
        tuple_term: The tuple.
        index: The position.
        alias: The alias.
    """

    function_name: ClassVar[str] = "tuple_element"

    def __init__(self, tuple_term: Any, index: int, alias: str | None = None) -> None:
        super().__init__(tuple_term, alias=alias)
        self.index = index
