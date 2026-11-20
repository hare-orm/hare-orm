from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from hare.exceptions import ConfigurationError
from hare.query.queryset.relations.related_queryset.relation_rows import RelationRows

if TYPE_CHECKING:
    from hare.core.hare_context import HareContext
    from hare.models.model import Model

TModel = TypeVar("TModel", bound="Model")


class InstanceAttachment:
    """Attach a detached loaded relation graph atomically, retaining its structural bindings."""

    @staticmethod
    def attach(context: HareContext, obj: TModel) -> TModel:
        """Validate all cached related objects before assigning their application context.

        Loaded values and the saved graph are preserved. Unfetched relations remain unfetched;
        their query objects are recreated under the selected context on subsequent access.
        """
        from hare.models.model import Model

        context.check_open()
        graph = obj._binding.graph
        pending: list[Model] = [obj]
        objects: list[Model] = []
        relation_rows: list[RelationRows] = []
        visited: set[int] = set()
        while pending:
            current = pending.pop()
            if id(current) in visited:
                continue
            visited.add(id(current))
            if current._binding.graph is not graph:
                raise ConfigurationError("Loaded relations belong to different model graphs")
            if current._context is not None and current._context is not context:
                raise ConfigurationError("The model instance already belongs to another application context")
            objects.append(current)
            binding = current._binding
            names = (
                *binding.foreign_key_fields,
                *binding.one_to_one_fields,
                *binding.backward_foreign_key_fields,
                *binding.backward_one_to_one_fields,
                *binding.many_to_many_fields,
                *binding.generic_foreign_key_fields,
            )
            for name in names:
                try:
                    value = object.__getattribute__(current, f"_{name}")
                except AttributeError:
                    continue
                if isinstance(value, Model):
                    pending.append(value)
                elif isinstance(value, RelationRows):
                    relation_rows.append(value)
                    pending.extend(value.related_objects)
        for current in objects:
            object.__setattr__(current, "_context", context)
        for rows in relation_rows:
            rows.held_relation = None
            rows.relation_reference = None
            rows.bound_settings = None
        return obj
