from __future__ import annotations

from typing import Any, ClassVar

from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_describer_compiler import PlanDescriberCompiler
from hare.query.plans.description.plan_description import PlanDescription


class Plannable:
    """A part of a query a plan can be kept for - an expression, a filter, a query built into
    another one. A class declares how each of its attributes meets the plan (``plan_parts``) and its
    description is generated from them; a class whose description no part type covers writes
    ``get_plan_description()`` itself; a class keeping no plan declares ``plannable = False`` - its
    parts then keep no plan, nor does the query they are in. A base class only its subclasses
    describe is declared with ``abstract=True`` among its class arguments.
    """

    #: Whether the class describes itself - declared False by a class that doesn't.
    plannable: ClassVar[bool] = True

    #: How each attribute meets the plan, in the order of the key - the description is generated
    #: from them (``PlanDescriberCompiler``). A subclass adding attributes declares them all again.
    plan_parts: ClassVar[DeclaredPlanParts]

    #: The attributes every object of a base class keeps that no SQL text depends on - bookkeeping
    #: the parts of its subclasses don't list.
    unplanned_attributes: ClassVar[tuple[str, ...]] = ()

    #: The object a part made again for each description or build stands for - set only on such a
    #: part (``PlanOrigins``); a slot of a class with slots.
    _plan_origin: Any

    __slots__ = ()

    def __init_subclass__(cls, abstract: bool = False, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "plan_parts" in cls.__dict__:
            if "get_plan_description" in cls.__dict__:
                raise TypeError(
                    f"{cls.__qualname__} declares plan_parts and get_plan_description() both - its description "
                    "is generated from its parts"
                )
            PlanDescriberCompiler.raise_if_slots_unclassified(cls)
            cls.get_plan_description = PlanDescriberCompiler.compile(cls)  # type: ignore[assignment, method-assign]
        describes_itself = cls.get_plan_description is not Plannable.get_plan_description
        if not cls.plannable:
            if describes_itself:
                raise TypeError(
                    f"{cls.__qualname__} declares plannable = False but describes a plan - a class keeping "
                    "no plan for some of its parts returns None from get_plan_description() for them"
                )
        elif not describes_itself and not abstract:
            raise TypeError(
                f"{cls.__qualname__} neither describes its plan (get_plan_description() or plan_parts) nor "
                "declares plannable = False"
            )

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """Describes this part for the plan of the query it is in.

        Args:
            context: What the part needs from the query.

        Returns:
            The description, None when the part keeps no plan.
        """
        return None
