from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.query.expressions import Q
from hare.query.scopes.row_visibility import RowVisibility

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class RowScope:
    """One default limit a model puts on the rows its queries see.

    Args:
        model: The model.
    """

    __slots__ = ("model",)

    def __init__(self, model: type[Model]) -> None:
        self.model = model

    def get_filter(self, visibility: RowVisibility) -> tuple[str, Any] | None:
        """The ``(field_name, value)`` filter the scope puts on a query of the model.

        Args:
            visibility: Which rows the query asks to see.

        Returns:
            The filter, or None when the scope is switched off or isn't a filter by one field.
        """
        return None

    def get_condition(self, visibility: RowVisibility) -> Q | None:
        """The condition the scope puts on the model's rows - the WHERE of a query of the model,
        the ON of a JOIN to it.

        Args:
            visibility: Which rows the query asks to see.

        Returns:
            The condition, or None when the scope doesn't limit the rows.
        """
        scope_filter = self.get_filter(visibility)
        return None if scope_filter is None else Q(**{scope_filter[0]: scope_filter[1]})
