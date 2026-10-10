from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import ValidationError
from hare.sql.terms.containers.constants import ARRAY_ELEMENT_INDEX_MAX, ARRAY_ELEMENT_INDEX_MIN
from hare.sql.terms.containers.container_function import ContainerFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field


class ArrayElementTerm(ContainerFunction):
    """One element of an array by its 0-based position - a negative one counts from the end; NULL
    past either end.

    Args:
        array: The array.
        index: The position.
        element_field: The field of the array's elements, when known - a dialect writes an element
            that is an array itself its own way.
        alias: The alias.

    Raises:
        ValidationError: ``index`` isn't an int in range.
    """

    function_name: ClassVar[str] = "array_element"

    def __init__(
        self, array: Any, index: int, element_field: Field[Any] | None = None, alias: str | None = None
    ) -> None:
        super().__init__(array, alias=alias)
        self.index = self.get_validated_index(index)
        self.element_field = element_field

    @staticmethod
    def get_validated_index(index: Any) -> int:
        """Checks a position before it is written into the SQL text.

        Args:
            index: The 0-based position.

        Returns:
            The position as a plain int.

        Raises:
            ValidationError: It isn't an int (a bool included) or is out of range.
        """
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValidationError(f"Array item index must be an int, got {type(index).__name__}")
        if not ARRAY_ELEMENT_INDEX_MIN <= index <= ARRAY_ELEMENT_INDEX_MAX:
            raise ValidationError(
                f"Array item index must be between {ARRAY_ELEMENT_INDEX_MIN} and {ARRAY_ELEMENT_INDEX_MAX}, "
                f"got {index}"
            )
        return int(index)
