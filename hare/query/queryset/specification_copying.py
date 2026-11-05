from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MemberDescriptorType
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.caching.cache import Cache
from hare.query.queryset.constants import UNCOPIED_QUERY_SLOTS

if TYPE_CHECKING:
    from hare.query.queryset.query_specification import QuerySpecification


class SpecificationCopying:
    """Copy declared query state without generating Python source.

    Structural slot names are cached per class. Mutable containers are copied according to
    their declared factories; nested prefetch lists are copied at the same boundary.
    """

    slot_names: ClassVar[Cache[Any]] = Cache(holds_sql=False)

    @staticmethod
    def get_slot_names(specification_class: type[QuerySpecification[Any]]) -> tuple[str, ...]:
        """Get the class's declared slots, excluding execution-origin bookkeeping.

        Args:
            specification_class: The class whose state is copied.

        Returns:
            The slot names, cached separately for each class.
        """
        cached = SpecificationCopying.slot_names.get_value(specification_class)
        if cached is None:
            names: dict[str, None] = {}
            for base in reversed(specification_class.__mro__):
                names.update(
                    (name, None)
                    for name, descriptor in vars(base).items()
                    if isinstance(descriptor, MemberDescriptorType) and name not in UNCOPIED_QUERY_SLOTS
                )
            cached = tuple(names)
            SpecificationCopying.slot_names.set_value(specification_class, cached)
        return cast("tuple[str, ...]", cached)

    @staticmethod
    def copy_state(
        source: QuerySpecification[Any],
        target: QuerySpecification[Any],
        specification_class: type[QuerySpecification[Any]] | None = None,
    ) -> None:
        """Copy the selected class's state into an already allocated query.

        Args:
            source: The state being copied.
            target: The independent destination.
            specification_class: Which declared slots to copy, defaulting to the source's class.
        """
        specification_class = type(source) if specification_class is None else specification_class
        # Bypass query subclasses' pickle hooks; Python supplies mangled names and omits unset slots.
        state = object.__getstate__(source)
        slot_values = state[1] if isinstance(state, tuple) else {}
        SpecificationCopying.copy_slots(
            {
                name: slot_values[name]
                for name in SpecificationCopying.get_slot_names(specification_class)
                if name in slot_values
            },
            target,
            specification_class.mutable_clone_slots,
        )
        source.copy_extension_state(target)

    @staticmethod
    def copy_slots(
        slot_values: Mapping[str, Any],
        target: QuerySpecification[Any],
        mutable_slots: Mapping[str, Callable[[], Any]],
    ) -> None:
        """Copy slot values with the declared container policy.

        Args:
            slot_values: Initialized slots selected for this copy.
            target: The independent destination.
            mutable_slots: Factories for empty mutable containers.
        """
        for name, value in slot_values.items():
            if name == "_prefetch_queries":
                value = {key: list(queries) for key, queries in value.items()}
            elif name == "_prefetch_map":
                value = {key: set(lookups) for key, lookups in value.items()}
            elif (factory := mutable_slots.get(name)) is not None:
                value = value.copy() if value else factory()
            setattr(target, name, value)

    @staticmethod
    def copy_specification(source: QuerySpecification[Any], target: QuerySpecification[Any]) -> None:
        """Hand a declaration's settings to a statement with independent mutable containers.

        Args:
            source: The query declaration.
            target: The statement being prepared.
        """
        SpecificationCopying.copy_slots(
            {name: getattr(source, name) for name in source.SPECIFICATION_SLOTS},
            target,
            source.MUTABLE_SPECIFICATION_SLOTS,
        )
