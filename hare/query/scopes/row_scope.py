from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.query.scopes.row_visibility import RowVisibility

if TYPE_CHECKING:
    from hare.models.model_binding import ModelBinding


class RowScope:
    """One default limit a model puts on the rows its queries see.

    Args:
        model: The model.
    """

    __slots__ = ("_binding",)

    def __init__(self, binding: ModelBinding) -> None:
        self._binding = binding

    def get_filter(self, visibility: RowVisibility) -> tuple[str, Any] | None:
        """The ``(field_name, value)`` filter the scope puts on a query of the model.

        Args:
            visibility: Which rows the query asks to see.

        Returns:
            The filter, or None when the scope is switched off or isn't a filter by one field.
        """
        return None
