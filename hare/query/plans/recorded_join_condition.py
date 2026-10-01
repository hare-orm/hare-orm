from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.scopes.row_visibility import RowVisibility


@dataclasses.dataclass(frozen=True, slots=True)
class RecordedJoinCondition:
    """A model's default scope a query folded into a JOIN while it recorded its plan - whose
    values a later query of the plan binds from its own context (the active tenant, say).

    Attributes:
        model: The joined model.
        visibility: The visibility the scope was read with, its tenant left to the active one
            unless the query pinned one - the plan key holds the rest.
        structure: The structure of the scope's condition when it was recorded, None when the
            scope had no condition (no tenant active and the tenant filter switched off, say).
        value_count: The number of its values.
    """

    model: type[Model]
    visibility: RowVisibility
    structure: Any
    value_count: int

    def get_values(self) -> list[Any] | None:
        """The values of the scope as the current context makes it.

        Returns:
            The values, in recording order - None when the scope has another structure now (a
            condition where there was none, another tenant filter shape), and the query has to
            be built again.

        Raises:
            QueryError: The model is tenant-scoped and no tenant is active.
        """
        # Local imports: the query package imports the plans package.
        from hare.query.plans.description.plan_context import PlanContext
        from hare.query.scopes.row_scopes import RowScopes

        condition = RowScopes.of(self.model).get_condition(self.visibility.get_for_active_tenant())
        if condition is None:
            return [] if self.structure is None else None
        description = condition.get_plan_description(PlanContext.EMPTY)
        if description is None or description.structure != self.structure:
            return None
        return description.values
