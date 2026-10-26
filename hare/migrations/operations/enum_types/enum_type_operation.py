from __future__ import annotations

from typing import Any

from hare.exceptions import ConfigurationError
from hare.migrations.operations.hare_operation import HareOperation


class EnumTypeOperation(HareOperation):
    """Base of the operations on a database ``ENUM`` type - named by its schema and name."""

    name: str
    schema: str | None
    backward_reads_state_left = False
    backward_reads_state_reached = False

    @staticmethod
    def get_checked_labels(labels: Any, label: str) -> tuple[str, ...]:
        """The labels of a type - texts, an empty one too, as a database with ENUM types takes it.

        Args:
            labels: The labels.
            label: The argument, as the message names it.

        Returns:
            The labels.

        Raises:
            ConfigurationError: ``labels`` isn't a list or tuple of texts.
        """
        if not isinstance(labels, (list, tuple)) or not all(isinstance(value, str) for value in labels):
            raise ConfigurationError(f"{label} takes a list of texts, got {labels!r}")
        return tuple(labels)

    def get_type_label(self) -> str:
        """The type as a migration describes it - with its schema when it has one."""
        return self.name if self.schema is None else f"{self.schema}.{self.name}"
