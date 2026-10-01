from __future__ import annotations

import dataclasses
from contextvars import ContextVar
from typing import TYPE_CHECKING, ClassVar

from hare.query.constants import AMBIENT_TENANT_NOT_OVERRIDDEN
from hare.query.plans.recorded_join_condition import RecordedJoinCondition

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.expressions.value_refs.value_ref_types import RecordedValueRefs
    from hare.query.plans.description.plan_description import PlanDescription
    from hare.query.scopes.row_visibility import RowVisibility


class JoinConditionRecording:
    """The conditions a query adds to its JOINs while building the statement it keeps as its plan -
    each model's default scope with the references of its values, so a later query of the plan binds
    its own scope. A None entry is a condition that can't be bound: the query keeps no plan.
    """

    #: The entries of the build recording now - set by the build itself - None while no build
    #: records.
    entries: ClassVar[ContextVar[list[tuple[RecordedJoinCondition, RecordedValueRefs] | None] | None]] = ContextVar(
        "hare_join_condition_entries", default=None
    )

    @classmethod
    def is_recording(cls) -> bool:
        """Whether a build records its join conditions now."""
        return cls.entries.get() is not None

    @classmethod
    def record_scope(
        cls,
        model: type[Model],
        visibility: RowVisibility,
        description: PlanDescription | None,
        value_wrapper_refs: RecordedValueRefs,
    ) -> None:
        """Records a model's default scope folded into a JOIN.

        Args:
            model: The joined model.
            visibility: The visibility the scope was read with.
            description: The scope condition's description - a description with no structure
                for a scope that had no condition; None when it keeps no plan.
            value_wrapper_refs: The references recorded while the condition was resolved.
        """
        entries = cls.entries.get()
        if entries is None:
            return
        if not visibility.tenant_is_pinned:
            # The active tenant is bound when the plan runs - it isn't part of the recording.
            visibility = dataclasses.replace(visibility, tenant=AMBIENT_TENANT_NOT_OVERRIDDEN)
        if (
            description is None
            or len(value_wrapper_refs) != len(description.values)
            or any(ref is None for _key, ref in value_wrapper_refs)
        ):
            entries.append(None)
            return
        entries.append(
            (
                RecordedJoinCondition(model, visibility, description.structure, len(description.values)),
                value_wrapper_refs,
            )
        )

    @classmethod
    def record_unbindable(cls) -> None:
        """Records a condition whose values no later query can bind - the query keeps no plan."""
        entries = cls.entries.get()
        if entries is not None:
            entries.append(None)
