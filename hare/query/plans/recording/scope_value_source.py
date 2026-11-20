from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, ClassVar

from hare.classes.deferred_import import DeferredImport
from hare.query.plans.description.plan_context import PlanContext

if TYPE_CHECKING:
    from hare.core.hare_context import HareContext
    from hare.models.model_binding import ModelBinding
    from hare.query.scopes.row_scopes import RowScopes
    from hare.query.scopes.row_visibility import RowVisibility


@dataclasses.dataclass(frozen=True, slots=True)
class ScopeValueSource:
    """A model's default scope a query folded into its JOINs while it recorded its plan - whose
    values a later query of the plan binds from its own context (the active tenant, say), into every
    JOIN it is folded into.

    Attributes:
        binding: The joined model's binding.
        visibility: The visibility the scope was read with, its tenant left to the active one
            unless the query pinned one - the plan key holds the rest.
        structure: The structure of the scope's condition when it was recorded, None when the
            scope had no condition (no tenant active and the tenant filter switched off, say).
        value_count: The number of its values.
    """

    binding: ModelBinding
    visibility: RowVisibility
    structure: Any
    value_count: int

    #: The scope class imports plan descriptions, so it is loaded on first use.
    row_scopes_class: ClassVar[DeferredImport[type[RowScopes]]]

    def get_values(self, *, application_context: HareContext | None = None) -> list[Any] | None:
        """The values of the scope as the current context makes it.

        Returns:
            The values, in the order the scope's description lists them - None when the scope has
            another structure now (a condition where there was none, another tenant filter shape),
            and the query has to be built again.

        Raises:
            QueryError: The model is tenant-scoped and no tenant is active.
        """
        condition = self.row_scopes_class.of(self.binding).get_condition(self.visibility)
        if condition is None:
            return [] if self.structure is None else None
        description = condition.get_plan_description(
            PlanContext(binding=self.binding, application_context=application_context)
        )
        if description is None or description.structure != self.structure:
            return None
        return description.values


DeferredImport("hare.query.scopes.row_scopes", "RowScopes").install_on(ScopeValueSource, "row_scopes_class")
