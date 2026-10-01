from __future__ import annotations

from typing import Any, ClassVar

from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription


class Plannable:
    """A part of a query a plan can be kept for - an expression, a filter, a query built into
    another one. A class describes itself with ``get_plan_description()`` or inherits a
    description, or declares ``plannable = False`` - its parts then keep no plan, nor does the
    query they are in. A base class only its subclasses describe is declared with
    ``abstract=True`` among its class arguments.
    """

    #: Whether the class describes itself - declared False by a class that doesn't.
    plannable: ClassVar[bool] = True

    __slots__ = ()

    def __init_subclass__(cls, abstract: bool = False, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        describes_itself = cls.get_plan_description is not Plannable.get_plan_description
        if not cls.plannable:
            if describes_itself:
                raise TypeError(
                    f"{cls.__qualname__} declares plannable = False but describes a plan - a class keeping "
                    "no plan for some of its parts returns None from get_plan_description() for them"
                )
        elif not describes_itself and not abstract:
            raise TypeError(
                f"{cls.__qualname__} neither describes its plan (get_plan_description()) nor declares "
                "plannable = False"
            )

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """Describes this part for the plan of the query it is in.

        Args:
            context: What the part needs from the query.

        Returns:
            The description, None when the part keeps no plan.
        """
        return None
