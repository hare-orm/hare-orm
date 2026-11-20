from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.models import Model
    from hare.models.model_binding import ModelBinding

#: Called with each event - a plain function, or an ``async def`` (an object with an async
#: ``__call__`` too).
ObserverCallback = Callable[[Any], "None | Awaitable[None]"]


@dataclasses.dataclass(slots=True)
class Observer:
    """One callback observing one event type.

    Attributes:
        callback: The callback.
        models: The models whose events it gets - their subclasses' too; None for every event.
        model_labels: The ``"app.Model"`` labels of more models whose events it gets - the model
            registered under the label in any context, its subclasses too.
    """

    callback: ObserverCallback
    models: frozenset[type[Model]] | None
    model_labels: frozenset[str] = frozenset()

    def observes(self, event: Any) -> bool:
        """Whether the event is one this observer gets.

        Args:
            event: The event.

        Returns:
            True for an observer of every event, or of the event's model or a base of it.
        """
        return self.models is None or self.observes_model(event.model, binding=getattr(event, "binding", None))

    def observes_model(self, model: type[Model], *, binding: ModelBinding | None = None) -> bool:
        """Whether this observer gets the events of ``model``.

        Args:
            model: The model.
            binding: The binding used to test the model and application subscription.

        Returns:
            True for an observer of every event, or of ``model`` or a base of it - by class or by label.
        """
        if self.models is None or any(issubclass(model, observed) for observed in self.models):
            return True
        if not self.model_labels:
            return False
        if binding is None:
            from hare.core.hare_context import HareContext

            context = HareContext.get_current()
            if context is not None and context.apps is not None:
                binding = context.apps.binding_graph.bindings.get(model)
        for base in model.__mro__:
            bound = None if binding is None else binding.graph.bindings.get(base)
            if bound is not None and bound.full_name in self.model_labels:
                return True
            declaration = getattr(base, "_declaration", None)
            if declaration is not None and declaration.app is not None:
                if f"{declaration.app}.{base.__name__}" in self.model_labels:
                    return True
        return False
