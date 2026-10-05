from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
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
        is_async: Whether calling it gives a coroutine.
    """

    callback: ObserverCallback
    models: frozenset[type[Model]] | None
    is_async: bool = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        # inspect.iscoroutinefunction() alone misses an object whose __call__ is async.
        self.is_async = inspect.iscoroutinefunction(self.callback) or inspect.iscoroutinefunction(
            type(self.callback).__call__
        )

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
            True for an observer of every event, or of ``model`` or a base of it.
        """
        return self.models is None or any(issubclass(model, observed) for observed in self.models)
