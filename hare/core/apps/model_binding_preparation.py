from __future__ import annotations

from collections.abc import Iterable
from types import TracebackType
from typing import TYPE_CHECKING, Self

from hare.core.apps.model_binding_graph import ModelBindingGraph
from hare.core.caching.cache import Cache
from hare.core.enums import ApplicationScope
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.core.apps.apps import Apps
    from hare.models.model import Model


class ModelBindingPreparation:
    """Prepare one registry version synchronously, publishing only its complete graph.

    Nested discovery/linking operations share the same draft. Failure restores the
    registry's previous model map; classes and previously published graphs are never
    modified. Connection resources remain owned by the caller.
    """

    def __init__(self, apps: Apps, models: Iterable[type[Model]] = ()) -> None:
        self.apps = apps
        self.models = tuple(models)
        self.parent: ModelBindingPreparation | None = None
        self.graph: ModelBindingGraph

    def __enter__(self) -> Self:
        parent = self.apps._preparation
        if parent is not None:
            self.parent = parent
            self.graph = parent.graph
            parent.include(self.models)
            return self
        self.previous_apps = self.apps.apps
        self.previous_graph = self.apps.binding_graph
        self.graph = ModelBindingGraph(self.previous_graph.dialects, swappable=self.apps.swappable_settings)
        self.apps._preparation = self
        self.apps.apps = {label: dict(models) for label, models in self.previous_apps.items()}
        # Registry preparation can import extensions: activate its application without pinning
        # an execution snapshot before those extensions have been loaded.
        from hare.core.hare_context import HareContext

        self.context_token = HareContext.current_context.set(
            self.apps.context if self.apps.context is not None else ApplicationScope.DETACHED
        )
        try:
            self.include((*self.apps.get_models_iterable(), *self.models))
        except BaseException:
            self.apps.apps = self.previous_apps
            self.apps._preparation = None
            HareContext.current_context.reset(self.context_token)
            raise
        return self

    def include(self, models: Iterable[type[Model]]) -> None:
        if self.parent is not None:
            self.parent.include(models)
            return
        for model in models:
            if model in self.graph.bindings:
                continue
            binding = self.graph.include(model)
            previous = self.previous_graph.bindings.get(model)
            if previous is not None:
                binding.app = previous.app
                binding.default_connection = previous.default_connection
                binding.managed = previous.managed
                binding.registered_live = previous.registered_live

    def __exit__(
        self, error_type: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None
    ) -> None:
        if self.parent is not None:
            return
        try:
            if error_type is None:
                try:
                    if set(self.graph.bindings) != set(self.apps.get_models_iterable()):
                        raise ConfigurationError("A prepared model graph must contain exactly its registered models")
                    self.graph.seal()
                except BaseException:
                    self.apps.apps = self.previous_apps
                    raise
                self.apps.binding_graph = self.graph
                # Only this registry's derivatives expire; historical registries sharing an
                # application do not invalidate its live manager or request-schema caches.
                Cache.clear_holder(self.apps)
            else:
                self.apps.apps = self.previous_apps
        finally:
            self.apps._preparation = None
            from hare.core.hare_context import HareContext

            HareContext.current_context.reset(self.context_token)
