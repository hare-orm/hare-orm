from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from hare.query.plans.description.plan_describer_compiler import PlanDescriberCompiler
from hare.query.plans.description.plannable import Plannable
from tests.plan_verification.exceptions import PlanMismatchError
from tests.plan_verification.plan_verification import PlanVerification


class PlanPartsVerification:
    """Checks that every attribute of an object described from its class's ``plan_parts`` is
    classified among them - an attribute added without saying how it meets the plan would leave it
    out of the key. A class with ``__slots__`` is checked when it is made
    (``PlanDescriberCompiler.raise_if_slots_unclassified()``); an object keeping its attributes in a
    ``__dict__`` is checked here, each time it is described.

    Installed for a pytest run with ``--verify-plans`` - a description made from parts each class
    already has is wrapped, and so is every one made later.
    """

    #: The attributes each class's parts classify.
    classified_by_class: ClassVar[dict[type, frozenset[str]]] = {}
    #: The compiler's ``compile()`` before it was patched.
    original_compile: ClassVar[Callable[[type], Any] | None] = None

    @classmethod
    def install(cls) -> None:
        """Wraps the descriptions made so far and every one made from now on."""
        if cls.original_compile is not None:
            return
        cls.original_compile = PlanDescriberCompiler.compile
        PlanDescriberCompiler.compile = staticmethod(cls.get_checked_compile(cls.original_compile))  # type: ignore[method-assign]
        for plannable_class in cls.get_plannable_classes(Plannable):
            if "plan_parts" in plannable_class.__dict__:
                plannable_class.get_plan_description = cls.get_checked_description(  # type: ignore[method-assign]
                    plannable_class.__dict__["get_plan_description"]
                )

    @classmethod
    def get_plannable_classes(cls, base: type) -> list[type]:
        """Every class made from ``base`` so far, at any depth.

        Args:
            base: The class.

        Returns:
            The classes.
        """
        found: list[type] = []
        for subclass in base.__subclasses__():
            found.append(subclass)
            found += cls.get_plannable_classes(subclass)
        return found

    @classmethod
    def get_checked_compile(cls, compile_description: Callable[[type], Any]) -> Callable[[type], Any]:
        """``PlanDescriberCompiler.compile()`` returning a checked description."""

        def compile_checked(plannable_class: type) -> Any:
            return cls.get_checked_description(compile_description(plannable_class))

        return compile_checked

    @classmethod
    def get_checked_description(cls, describe: Callable[[Any, Any], Any]) -> Callable[[Any, Any], Any]:
        """A generated description checking the attributes of the object it describes first."""

        def describe_checked(plannable: Any, context: Any) -> Any:
            attributes = getattr(plannable, "__dict__", None)
            if attributes:
                unclassified = set(attributes) - cls.get_classified(type(plannable))
                if unclassified:
                    mismatch = (
                        f"{type(plannable).__qualname__}.plan_parts classifies none of its attributes "
                        f"{sorted(unclassified)}"
                    )
                    PlanVerification.mismatches.append(mismatch)
                    raise PlanMismatchError(mismatch)
            return describe(plannable, context)

        return describe_checked

    @classmethod
    def get_classified(cls, plannable_class: type) -> frozenset[str]:
        """The attributes a class's parts classify - with its base classes' bookkeeping and the
        origin a copy keeps.

        Args:
            plannable_class: The class.

        Returns:
            The attribute names.
        """
        classified = cls.classified_by_class.get(plannable_class)
        if classified is None:
            classified = cls.classified_by_class[plannable_class] = frozenset(
                [
                    *(attribute for attribute, _part_type in plannable_class.plan_parts),  # type: ignore[attr-defined]
                    *plannable_class.unplanned_attributes,  # type: ignore[attr-defined]
                    *PlanDescriberCompiler.ORIGIN_SLOTS,
                ]
            )
        return classified
