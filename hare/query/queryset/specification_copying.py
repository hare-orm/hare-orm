from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.caching.cache import Cache
from hare.query.queryset.constants import UNCOPIED_QUERY_SLOTS

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.query_specification import QuerySpecification


class SpecificationCopying:
    """How a query specification is copied: per class, a generated function assigning each slot one
    by one - which the interpreter specializes, unlike ``getattr``/``setattr`` by name - a slot
    holding a mutable container getting its own copy; and the settings of a specification handed to a
    query built from it."""

    #: Every ``__slots__`` name across a specification class's MRO - kept on the class in
    #: ``clone_slots_cache``.
    slot_names: ClassVar[Cache[Any]] = Cache(
        holds_sql=False, keyed_by_model=False, owner_attribute="clone_slots_cache"
    )
    #: A specification class's compiled copy function - kept on the class in ``compiled_copy_cache``.
    compiled_copies: ClassVar[Cache[Any]] = Cache(
        holds_sql=False, keyed_by_model=False, owner_attribute="compiled_copy_cache"
    )

    #: The generated function handing a specification's settings to a query - see ``copy_specification()``.
    specification_copy_function: ClassVar[Callable[[Any, Any], None] | None] = None

    @staticmethod
    def get_slot_names(specification_class: type[QuerySpecification[Any]]) -> tuple[str, ...]:
        """Every ``__slots__`` name across the class's MRO a copy takes over, cached per class - what its
        copy function is generated from.

        Args:
            specification_class: The specification class.

        Returns:
            The slot names.
        """
        cached = specification_class.__dict__.get("clone_slots_cache")
        if cached is None:
            cached = tuple(
                {slot for klass in specification_class.__mro__ for slot in klass.__dict__.get("__slots__", ())}
                - UNCOPIED_QUERY_SLOTS
            )
            SpecificationCopying.slot_names.set_owner_value(specification_class, cached)
        return cast("tuple[str, ...]", cached)

    @staticmethod
    def compile_copy(specification_class: type[QuerySpecification[Any]]) -> Callable[[Any, Any], None]:
        """Compiles, once per class, ``def _copy(self, newone): newone.a = self.a; ...`` - a slot in
        ``mutable_clone_slots`` assigned its ``.copy()``.

        Args:
            specification_class: The specification class.

        Returns:
            The copy function.
        """
        cached = specification_class.__dict__.get("compiled_copy_cache")
        if cached is not None:
            return cached
        mutable_slots = specification_class.mutable_clone_slots
        lines = [
            SpecificationCopying.get_slot_copy_source(name, "self", "newone", mutable_slots)
            for name in SpecificationCopying.get_slot_names(specification_class)
        ]
        body = "\n".join(lines) or "    pass"
        namespace: dict[str, Any] = {}
        exec(f"def _copy(self, newone):\n{body}", namespace)  # nosec B102 - class-derived source, no external input
        compiled: Callable[[Any, Any], None] = namespace["_copy"]
        SpecificationCopying.compiled_copies.set_owner_value(specification_class, compiled)
        return compiled

    @staticmethod
    def get_slot_copy_source(name: str, source: str, target: str, mutable_slots: dict[str, str]) -> str:
        """The line of a compiled copy function copying one slot: a mutable container gets its own
        copy - a new empty one when it is empty, without a call.

        Args:
            name: The slot.
            source: The name of the object copied from.
            target: The name of the object copied to.
            mutable_slots: The slots holding a mutable container, each with the source of an empty
                container of its type.

        Returns:
            The line.
        """
        empty_source = mutable_slots.get(name)
        if empty_source is None:
            return f"    {target}.{name} = {source}.{name}"
        return f"    {target}.{name} = {source}.{name}.copy() if {source}.{name} else {empty_source}"

    @staticmethod
    def copy_specification(source: QuerySpecification[Any], target: QuerySpecification[Any]) -> None:
        """Hands every setting of a specification to a query built from it - a container the build
        changes as its own copy.

        Args:
            source: The specification.
            target: The query.
        """
        copy_function = SpecificationCopying.specification_copy_function
        if copy_function is None:
            # One direct attribute assignment per slot, like compile_copy().
            lines = [
                SpecificationCopying.get_slot_copy_source(name, "source", "target", source.MUTABLE_SPECIFICATION_SLOTS)
                for name in source.SPECIFICATION_SLOTS
            ]
            namespace: dict[str, Any] = {}
            exec("def copy_specification(source, target):\n" + "\n".join(lines), namespace)  # nosec B102 - class-derived source, no external input
            copy_function = SpecificationCopying.specification_copy_function = cast(
                "Callable[[Any, Any], None]", namespace["copy_specification"]
            )
        copy_function(source, target)
        prefetch_queries = source._prefetch_queries
        target._prefetch_queries = (
            {key: list(value) for key, value in prefetch_queries.items()} if prefetch_queries else {}
        )
