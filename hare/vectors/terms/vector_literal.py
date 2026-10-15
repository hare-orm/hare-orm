from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function


class VectorLiteral(Function):
    """A vector bound as a parameter - each dialect writes the parameter as its vector type takes it.

    Args:
        value: The bound value - the vector as the dialect stores it.
    """

    requires_dialect_renderer = True

    def __init__(self, value: Any) -> None:
        super().__init__("VECTOR_LITERAL", value)
