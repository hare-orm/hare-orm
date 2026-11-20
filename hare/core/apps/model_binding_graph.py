from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError
from hare.models.model_binding import ModelBinding

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.models.model import Model


class ModelBindingGraph:
    """Structural model bindings shared by the queries prepared from one registry version.

    A graph contains no registry facade, context or connection resources. Preparation
    allocates all nodes before linking them, then seals its mapping. Existing queries
    retain their graph when a registry publishes another version.
    """

    def __init__(
        self,
        dialects: Mapping[str, Dialect] | None = None,
        *,
        swappable: Mapping[str, str | dict[str, str]] | None = None,
    ) -> None:
        self.dialects = MappingProxyType(dict(dialects or {}))
        self.swappable = MappingProxyType(
            {name: dict(value) if isinstance(value, dict) else value for name, value in (swappable or {}).items()}
        )
        self.bindings: Mapping[type[Model], ModelBinding] = {}
        self.sealed = False

    def include(self, model: type[Model]) -> ModelBinding:
        """Allocate an unpublished node from its original class declaration."""
        binding = self.bindings.get(model)
        if binding is None:
            if self.sealed:
                raise ConfigurationError(f"Model {model.__name__!r} is not registered in this model graph")
            binding = ModelBinding.from_declaration(model._declaration, self)
            self.bindings[model] = binding  # type: ignore[index]
        return binding

    def get_binding(self, model: type[Model]) -> ModelBinding:
        """Read an existing node without consulting the active application."""
        try:
            return self.bindings[model]
        except KeyError:
            raise ConfigurationError(f"Model {model.__name__!r} is not registered in this model graph") from None

    def get_dialect(self, connection_alias: str | None) -> Dialect:
        """Read configured SQL metadata without retaining or selecting a database client."""
        from hare.dialects.base.constants import SQL_DIALECT

        return self.dialects.get(connection_alias, SQL_DIALECT)  # type: ignore[arg-type]

    def check_published(self) -> None:
        """Refuse IO from declarations or instances escaping an unfinished preparation."""
        if not self.sealed:
            raise ConfigurationError("An unpublished model graph cannot execute database operations")

    def seal(self) -> None:
        """Publish node identities after the complete graph has passed its checks."""
        for binding in self.bindings.values():
            for field in (*binding.fields_map.values(), *binding.hidden_backward_relations.values()):
                relation = field.get_binding().relation
                if relation is None:
                    continue
                if relation.related_model is not None:
                    relation.related_binding = self.get_binding(relation.related_model)
                if relation.through_model_class is not None:
                    relation.through_binding = self.get_binding(relation.through_model_class)
        self.bindings = MappingProxyType(dict(self.bindings))
        self.sealed = True

    def __copy__(self) -> ModelBindingGraph:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> ModelBindingGraph:
        memo[id(self)] = self
        return self

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        """Save importable declarations and effective placement, excluding all execution resources."""
        if not self.sealed:
            raise ConfigurationError("Only a completely prepared model graph can be serialized")
        placements = tuple(
            (
                model,
                binding.app,
                binding.default_connection,
                binding.managed,
                binding.registered_live,
                binding.db_table,
                binding.generated_db_table,
                binding.schema,
            )
            for model, binding in self.bindings.items()
        )
        dialects = tuple((alias, dialect.name) for alias, dialect in self.dialects.items())
        # Schema declarations are captured on the class; normal preparation replays
        # their extension protocols instead of inspecting concrete projection internals.
        return ModelBindingGraph.restore, (placements, dialects, dict(self.swappable))

    @staticmethod
    def get_saved_binding(graph: ModelBindingGraph, model: type[Model]) -> ModelBinding:
        """Reuse one restored graph for all instances in the same pickle stream."""
        return graph.get_binding(model)

    @staticmethod
    def restore(
        placements: tuple[tuple[Any, ...], ...],
        dialects: tuple[tuple[str, str], ...],
        swappable: dict[str, str | dict[str, str]],
    ) -> ModelBindingGraph:
        """Rebuild through ordinary relation preparation without configuring or opening clients."""
        from hare.core.apps.apps import Apps
        from hare.core.apps.model_binding_preparation import ModelBindingPreparation
        from hare.core.connections.connection_handler import ConnectionHandler
        from hare.dialects.dialect_registry import DialectRegistry

        names = {model: generated or table for model, _, _, _, _, table, generated, _ in placements}
        apps = Apps(
            {},
            ConnectionHandler(),
            names.__getitem__,
            validate_connections=False,
            swappable=swappable,
            dialects={alias: DialectRegistry.get_dialect(name) for alias, name in dialects},
        )
        with ModelBindingPreparation(apps, (placement[0] for placement in placements)):
            for model, app, alias, managed, registered_live, table, generated, schema in placements:
                apps.apps.setdefault(app, {})[model.__name__] = model
                binding = apps.get_binding(model)
                binding.app = app
                binding.default_connection = alias
                binding.managed = managed
                binding.registered_live = registered_live
                binding.db_table = table
                binding.generated_db_table = generated
                binding.schema = schema
            apps.finish_model_binding()
        return apps.binding_graph
