from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hare.declarations.declaration_value_serializer import DeclarationValueSerializer


@dataclass(frozen=True)
class InspectedFieldSpecification:
    """A field an introspected type maps to, held in an argument of another field - the element of an
    array, the key and value of a map, the elements of a tuple - itself holding fields to any depth.

    Attributes:
        path: The dotted path of the field class.
        kwargs: Its keyword arguments - fields among them as specs too.
        is_ambiguous: The field is a fallback for an unrecognized physical type.
    """

    path: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    is_ambiguous: bool = False

    def build(self) -> Any:
        """The field the spec describes.

        Returns:
            The field.
        """
        return DeclarationValueSerializer.get_callable(self.path)(**self.build_arguments(self.kwargs))

    @staticmethod
    def build_arguments(value: Any) -> Any:
        """Arguments with every spec among them - in a list, a tuple or a dict too - built.

        Args:
            value: The arguments, or one of them.

        Returns:
            The same shape with the fields built.
        """
        if isinstance(value, InspectedFieldSpecification):
            return value.build()
        if isinstance(value, dict):
            return {key: InspectedFieldSpecification.build_arguments(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(InspectedFieldSpecification.build_arguments(item) for item in value)
        return value
