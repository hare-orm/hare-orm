from __future__ import annotations

import dataclasses
from contextvars import ContextVar
from functools import partial
from typing import TYPE_CHECKING, ClassVar

from hare.query.constants import AMBIENT_TENANT_NOT_OVERRIDDEN
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.plan_origins import PlanOrigins
from hare.query.plans.recording.scope_value_source import ScopeValueSource

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.expressions.conditions.q import Q
    from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences, ValueReference
    from hare.query.scopes.row_visibility import RowVisibility


class PlanRecording:
    """What the query building the statement it keeps as its plan records of the conditions the build
    folds into JOINs, deep where the query's own references aren't at hand: the references of the
    values of its own conditions - a ``FilteredRelation``'s, a ``Select(relation, extra_condition=...)``
    - each under the origin of its value, as the query's description lists them; and the default
    scopes, whose values a later query of the plan binds from its own context (``ScopeValueSource``).
    """

    #: The recording of the build recording now - set by the build itself - None while no build
    #: records.
    current: ClassVar[ContextVar[PlanRecording | None]] = ContextVar("hare_plan_recording", default=None)

    __slots__ = ("join_condition_references", "keeps_plan", "scope_references")

    def __init__(self) -> None:
        #: The references of the values of the query's own conditions folded into JOINs.
        self.join_condition_references: RecordedValueReferences = []
        #: Per default scope folded, the references of each of its values, in the order its
        #: description lists them - one scope folded into several JOINs binds its values into each.
        self.scope_references: dict[ScopeValueSource, list[list[ValueReference]]] = {}
        #: False once a scope is folded that no later query can bind - the query keeps no plan.
        self.keeps_plan = True

    @classmethod
    def is_recording(cls) -> bool:
        """Whether a build records its plan now."""
        return cls.current.get() is not None

    @classmethod
    def record_join_condition(cls, value_wrapper_references: RecordedValueReferences) -> None:
        """Records the references a condition of the recording query folded into a JOIN recorded -
        the query's description lists its values; a value of one folded into no JOIN binds nothing.

        Args:
            value_wrapper_references: The references recorded while the condition was resolved.
        """
        recording = cls.current.get()
        if recording is not None:
            recording.join_condition_references += value_wrapper_references

    @classmethod
    def record_scope(
        cls,
        model: type[Model],
        visibility: RowVisibility,
        condition: Q | None,
        value_wrapper_references: RecordedValueReferences,
    ) -> None:
        """Records a model's default scope folded into a JOIN, each reference under the origin of its
        value.

        Args:
            model: The joined model.
            visibility: The visibility the scope was read with.
            condition: The scope's condition, None for a scope that had none.
            value_wrapper_references: The references recorded while the condition was resolved.
        """
        recording = cls.current.get()
        if recording is None:
            return
        if not visibility.tenant_is_pinned:
            # The active tenant is bound when the plan runs - it isn't part of the recording.
            visibility = dataclasses.replace(visibility, tenant=AMBIENT_TENANT_NOT_OVERRIDDEN)
        description = (
            PlanDescription.ABSENT
            if condition is None
            else PlanOrigins.describe(partial(condition.get_plan_description, PlanContext(model=model)))
        )
        if description is None or description.origins is None:
            recording.keeps_plan = False
            return
        source = ScopeValueSource(model, visibility, description.structure, len(description.values))
        position_by_origin = {origin: position for position, origin in enumerate(description.origins)}
        references_by_position = recording.scope_references.setdefault(source, [[] for _origin in description.origins])
        for origin, reference in value_wrapper_references:
            position = position_by_origin.get(origin)
            if reference is None or position is None:
                recording.keeps_plan = False
                return
            references_by_position[position].append(reference)
