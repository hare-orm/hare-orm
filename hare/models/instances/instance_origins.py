from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import ValidationError

if TYPE_CHECKING:
    from hare.core.hare_context import HareContext
    from hare.models.model import Model
    from hare.models.model_binding import ModelBinding


class InstanceOrigins:
    """Check structural and application identity before an operation consumes instance keys."""

    @staticmethod
    def check(binding: ModelBinding, context: HareContext | None, obj: Model, operation: str) -> None:
        """Reject an instance belonging to another graph or an explicitly different application."""
        if obj._binding is not binding or (context is not None and obj._context is not context):
            raise ValidationError(
                f"{operation} cannot mix instances from different model graphs or application contexts"
            )
