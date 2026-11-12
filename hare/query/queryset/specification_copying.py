from __future__ import annotations

from collections.abc import Callable
from types import MemberDescriptorType
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.cache import Cache
from hare.native.native_modules import NativeModules
from hare.query.queryset.constants import UNCOPIED_QUERY_SLOTS

if TYPE_CHECKING:
    from hare.query.queryset.query_specification import QuerySpecification


class SpecificationCopying:
    """Copy declared query state without generating Python source.

    Structural slot names are cached per class. Mutable containers are copied according to
    their declared factories; nested prefetch lists are copied at the same boundary.
    """

    #: The declared slots of each class (``get_slot_names()``).
    slot_names: ClassVar[Cache[Any]] = Cache(holds_sql=False)
    #: How each class's slots are copied (``get_slot_copy_plan()``).
    slot_copy_plans: ClassVar[Cache[Any]] = Cache(holds_sql=False)
    #: ``QuerySet`` - imported at the first copy, as its module imports this one.
    query_set_class: ClassVar[type[Any] | None] = None
    #: rust.native.rows.copy_slot_values() - copies a query's slots to its clone.
    slot_copier: ClassVar[Any] = NativeModules.rows.copy_slot_values

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
        return cached

    @staticmethod
    def get_slot_copy_plan(
        specification_class: type[QuerySpecification[Any]],
    ) -> tuple[tuple[str, ...], tuple[tuple[str, Callable[[], Any]], ...], bool, bool]:
        """Get the plain slots, mutable container slots and prefetch flags for ``specification_class``."""
        bucket = specification_class.copy_plan_bucket
        cached = bucket.get(None)
        if cached is None:
            mutable_slots = specification_class.mutable_clone_slots
            plain_names: list[str] = []
            container_slots: list[tuple[str, Callable[[], Any]]] = []
            has_prefetch_map = False
            has_prefetch_queries = False
            for name in SpecificationCopying.get_slot_names(specification_class):
                if name == "_prefetch_map":
                    has_prefetch_map = True
                elif name == "_prefetch_queries":
                    has_prefetch_queries = True
                elif (factory := mutable_slots.get(name)) is not None:
                    container_slots.append((name, factory))
                else:
                    plain_names.append(name)
            cached = (tuple(plain_names), tuple(container_slots), has_prefetch_map, has_prefetch_queries)
            bucket[None] = cached
        return cached

    @staticmethod
    def copy_state(
        source: QuerySpecification[Any],
        target: QuerySpecification[Any],
        specification_class: type[QuerySpecification[Any]] | None = None,
    ) -> None:
        """Copy the selected class's state into an already allocated query.

        The selected class's slots are copied in its slot order - a slot the source never set stays
        unset - preserving custom setters and transfers between different query classes. A
        destination QuerySet always loses its unchanged-manager identity: its caller may configure
        it after this transfer, including when the call signature is preserved.

        Args:
            source: The state being copied.
            target: The independent destination.
            specification_class: Which declared slots to copy, defaulting to the source's class.
        """
        query_set_class = SpecificationCopying.query_set_class
        if query_set_class is None:
            # Deferred import: the queryset module imports this one.
            from hare.query.queryset.queryset import QuerySet

            SpecificationCopying.query_set_class = query_set_class = QuerySet
        plain_names, container_slots, has_prefetch_map, has_prefetch_queries = SpecificationCopying.get_slot_copy_plan(
            type(source) if specification_class is None else specification_class
        )
        # One native call over the slots - getattr/setattr each, a slot never set skipped, a container copied.
        SpecificationCopying.slot_copier(source, target, plain_names, container_slots)
        if has_prefetch_map:
            prefetch_map = getattr(source, "_prefetch_map", None)
            if prefetch_map is not None:
                target._prefetch_map = {key: set(lookups) for key, lookups in prefetch_map.items()}
        if has_prefetch_queries:
            prefetch_queries = getattr(source, "_prefetch_queries", None)
            if prefetch_queries is not None:
                target._prefetch_queries = {key: list(queries) for key, queries in prefetch_queries.items()}
        if isinstance(target, query_set_class):
            target._unchanged_base_manager = None
        source.copy_extension_state(target)

    @staticmethod
    def share_specification(source: QuerySpecification[Any], target: QuerySpecification[Any]) -> None:
        """Hand a declaration's settings to a statement by reference - its containers stay the
        declaration's own until the statement is copied (``copy_state()`` gives the copy independent
        ones), so nothing changes them in place before that.

        Args:
            source: The query declaration.
            target: The statement reading it.
        """
        for name in source.SPECIFICATION_SLOTS:
            setattr(target, name, getattr(source, name))

    @staticmethod
    def copy_specification(source: QuerySpecification[Any], target: QuerySpecification[Any]) -> None:
        """Hand a declaration's settings to a statement with independent mutable containers.

        Args:
            source: The query declaration.
            target: The statement being prepared.
        """
        mutable_slots = source.MUTABLE_SPECIFICATION_SLOTS
        for name in source.SPECIFICATION_SLOTS:
            value = getattr(source, name)
            if (factory := mutable_slots.get(name)) is not None:
                if not value:
                    value = factory()
                elif name == "_prefetch_map":
                    value = {key: set(lookups) for key, lookups in value.items()}
                else:
                    value = value.copy()
            elif name == "_prefetch_queries":
                value = {key: list(queries) for key, queries in value.items()} if value else {}
            setattr(target, name, value)
