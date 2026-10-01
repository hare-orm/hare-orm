from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    pass


class DialectImplementedOperators:
    """Marks the lookup operators only a dialect implements: the operator's own body raises, and a
    dialect replaces it in its ``FilterOperators``. A marked operator a dialect didn't replace isn't
    supported there - a query using it fails with ``UnSupportedError`` before its SQL is built.
    """

    #: The attribute a marked operator carries.
    MARK: ClassVar[str] = "dialect_implemented"

    @classmethod
    def mark(cls, operator_function: Callable[..., Any]) -> Callable[..., Any]:
        """Marks an operator as implemented only by dialects.

        Args:
            operator_function: The operator.

        Returns:
            The same operator.
        """
        setattr(operator_function, cls.MARK, True)
        return operator_function

    @classmethod
    def is_marked(cls, operator_function: Callable[..., Any]) -> bool:
        """Whether an operator is implemented only by dialects.

        Args:
            operator_function: The operator.

        Returns:
            True for a marked operator.
        """
        return getattr(operator_function, cls.MARK, False) is True
