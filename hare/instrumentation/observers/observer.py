from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.models import Model

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
        return self.models is None or self.observes_model(event.model)

    def observes_model(self, model: type[Model]) -> bool:
        """Whether this observer gets the events of ``model``.

        Args:
            model: The model.

        Returns:
            True for an observer of every event, or of ``model`` or a base of it - by class or by label.
        """
        if self.models is None or any(issubclass(model, observed) for observed in self.models):
            return True
        return bool(self.model_labels) and any(
            getattr(getattr(base, "_meta", None), "full_name", None) in self.model_labels for base in model.__mro__
        )
