from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class InspectedFieldSpecification:
    """A field an introspected type maps to, held in an argument of another field - the element of an
    array, the key and value of a map, the elements of a tuple - itself holding fields to any depth.

    Attributes:
        path: The dotted path of the field class.
        kwargs: Its keyword arguments - fields among them as specs too.
    """

    path: str
    kwargs: dict[str, Any] = field(default_factory=dict)

    def build(self) -> Any:
        """The field the spec describes.

        Returns:
            The field.
        """
        # Local import: the migration writer imports the inspectdb package.
        from hare.migrations.writer.migration_writer import MigrationWriter

        return MigrationWriter.get_callable(self.path)(**self.build_arguments(self.kwargs))

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
