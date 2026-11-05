from __future__ import annotations

from collections.abc import Iterable
from contextvars import ContextVar, Token
from types import TracebackType
from typing import TYPE_CHECKING, ClassVar, Self

from hare.core.apps.binding_copies import BindingCopies
from hare.core.apps.binding_delta import BindingDelta
from hare.core.caching.caches import Caches

if TYPE_CHECKING:
    from hare.core.apps.apps import Apps
    from hare.models import Model


class ModelBindingGraph:
    """Prepare registry metadata as one graph and publish after all checks succeed.

    Binding is synchronous: no database IO or task suspension is allowed inside this
    boundary. Model classes expose the preparation graph to binding hooks while they run;
    references retained to the previous MetaInfo and fields remain unchanged. On failure,
    the registry and every class namespace are restored. Arbitrary side effects performed
    by user callbacks outside these namespaces are not transactional.
    """

    current: ClassVar[ContextVar[ModelBindingGraph | None]] = ContextVar("model_binding_graph", default=None)

    def __init__(self, apps: Apps, models: Iterable[type[Model]] = ()) -> None:
        self.apps = apps
        self.models = tuple(models)
        self.copies = BindingCopies()
        self.deltas: dict[type[Model], BindingDelta] = {}
        self.prepared: set[type[Model]] = set()
        self.previous_apps = apps.apps
        self.parent: ModelBindingGraph | None = None
        self.token: Token[ModelBindingGraph | None] | None = None

    def __enter__(self) -> Self:
        parent = self.current.get()
        if parent is not None and parent.apps is self.apps:
            self.parent = parent
            parent.include(self.models)
            return self
        self.token = self.current.set(self)
        self.apps.apps = {label: dict(models) for label, models in self.previous_apps.items()}
        try:
            self.include((*self.apps.get_models_iterable(), *self.models))
        except BaseException:
            self.restore()
            self.current.reset(self.token)
            raise
        return self

    def include(self, models: Iterable[type[Model]]) -> None:
        """Add discovered models before changing their labels, fields or connection."""
        if self.parent is not None:
            self.parent.include(models)
            return
        for model in models:
            if model in self.prepared:
                continue
            pending = [model]
            while pending:
                descendant = pending.pop()
                if descendant not in self.deltas:
                    self.deltas[descendant] = BindingDelta(descendant)
                    pending.extend(descendant.__subclasses__())
            self.prepared.add(model)
            model._meta = self.copies.copy(model._meta)
            for name, field in model._meta.generic_foreign_key_fields.items():
                type.__setattr__(model, name, field)

    def restore(self) -> None:
        for delta in self.deltas.values():
            delta.restore()
        self.apps.apps = self.previous_apps

    def __exit__(
        self, error_type: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None
    ) -> None:
        if self.parent is not None:
            return
        try:
            if error_type is None:
                from hare.query.queryset.pending_calls.calls_before_setup import CallsBeforeSetup

                CallsBeforeSetup.apply_pending()
                for delta in self.deltas.values():
                    delta.capture()
                prepared_apps = self.apps.apps
                self.restore()
                for delta in self.deltas.values():
                    delta.publish()
                self.apps.apps = prepared_apps
            else:
                self.restore()
        except BaseException:
            self.restore()
            raise
        finally:
            try:
                Caches.forget_model_caches(self.prepared)
            finally:
                if self.token is not None:
                    self.current.reset(self.token)
