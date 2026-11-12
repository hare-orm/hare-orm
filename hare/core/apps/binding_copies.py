from __future__ import annotations

from typing import Any


class BindingCopies:
    """Copy framework binding state without copying models, callbacks or external resources.

    One memo covers every model of a binding attempt, so a shadow column and a relation's
    target key keep pointing at the same copied field. This is a preparation workspace,
    not a cache. Declaration snapshots remain shared and are never modified by binding.
    """

    def __init__(self) -> None:
        # Binding starts after model classes are available. Keep these types for the
        # whole workspace rather than importing them for every value in its graph.
        from hare.ddl.indexes.index import Index
        from hare.fields.bound_field import BoundField
        from hare.fields.field import Field
        from hare.fields.relations.bound_relation import BoundRelation
        from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance
        from hare.instrumentation.capture.change_sink import ChangeSink
        from hare.models.meta_info import MetaInfo

        self.copies: dict[int, Any] = {}
        self.framework_classes = (
            Field,
            BoundField,
            BoundRelation,
            GenericForeignKeyFieldInstance,
            MetaInfo,
            ChangeSink,
            Index,
        )
        self.bound_field_class = BoundField

    def copy(self, value: Any) -> Any:
        identity = id(value)
        if identity in self.copies:
            return self.copies[identity]
        if isinstance(value, dict):
            result: Any = {}
            self.copies[identity] = result
            result.update((self.copy(key), self.copy(item)) for key, item in value.items())
        elif isinstance(value, list):
            result = []
            self.copies[identity] = result
            result.extend(self.copy(item) for item in value)
        elif isinstance(value, set):
            result = set()
            self.copies[identity] = result
            result.update(self.copy(item) for item in value)
        elif isinstance(value, (tuple, frozenset)):
            items = [self.copy(item) for item in value]
            # An indirect cycle may already have completed this immutable container.
            if identity in self.copies:
                return self.copies[identity]
            result = tuple(items) if isinstance(value, tuple) else frozenset(items)
            self.copies[identity] = result
        elif isinstance(value, self.framework_classes):
            result = object.__new__(type(value))
            self.copies[identity] = result
            state = object.__getstate__(value)
            if isinstance(state, tuple):
                attributes, slots = state
                attributes = {**(attributes or {}), **slots}
            else:
                attributes = state or {}  # type: ignore[assignment]
            for name, item in attributes.items():
                if name in ("cache_buckets", "plan_cache_buckets"):
                    copied = {}
                elif isinstance(value, self.bound_field_class) and name == "declaration":
                    copied = item
                else:
                    copied = self.copy(item)
                object.__setattr__(result, name, copied)
        else:
            return value
        return result
