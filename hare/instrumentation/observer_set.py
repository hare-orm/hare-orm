from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import QueryError
from hare.instrumentation.observer import Observer, ObserverCallback

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class ObserverSet:
    """The observers of one scope - the process (``Observers``) or one ``HareContext`` - by event
    type, each event type's in order of registration."""

    #: How many observers every set holds together - with none anywhere, an event costs nothing.
    total_count: ClassVar[int] = 0

    def __init__(self) -> None:
        self.observers_by_event_type: dict[type, dict[ObserverCallback, Observer]] = {}

    def add(self, event_type: type, callback: ObserverCallback, models: Iterable[type[Model]] | None) -> None:
        """Adds an observer. Adding a callback again adds the models to the ones it observes; an
        observer of every event stays one, and adding it without models makes it one.

        Args:
            event_type: The event type.
            callback: The callback.
            models: The models whose events it gets, None for every event.

        Raises:
            QueryError: ``event_type`` isn't an event narrowed by model.
            TypeError: A model isn't a model class.
        """
        model_set = ObserverSet.get_model_set(event_type, models)
        observers = self.observers_by_event_type.setdefault(event_type, {})
        existing = observers.get(callback)
        if existing is None:
            observers[callback] = Observer(callback, model_set)
            ObserverSet.total_count += 1
        elif existing.models is not None:
            existing.models = None if model_set is None else existing.models | model_set

    def remove(self, event_type: type, callback: ObserverCallback, models: Iterable[type[Model]] | None) -> None:
        """Stops a callback observing the events of ``models`` - removes it entirely without them,
        or once it observes no model; nothing when it doesn't observe the event type.

        Args:
            event_type: The event type.
            callback: The callback.
            models: The models it stops observing, None to remove it.

        Raises:
            QueryError: The callback observes every event - it can only be removed entirely.
            TypeError: A model isn't a model class.
        """
        model_set = ObserverSet.get_model_set(event_type, models)
        observers = self.observers_by_event_type.get(event_type, {})
        existing = observers.get(callback)
        if existing is None:
            return
        if model_set is not None:
            if existing.models is None:
                raise QueryError(f"{callback!r} observes every event - unobserve it without models to remove it")
            existing.models -= model_set
            if existing.models:
                return
        del observers[callback]
        ObserverSet.total_count -= 1

    def clear(self) -> None:
        """Removes every observer."""
        ObserverSet.total_count -= sum(len(observers) for observers in self.observers_by_event_type.values())
        self.observers_by_event_type.clear()

    def get_observers(self, event: Any) -> list[Observer]:
        """The observers that get ``event``.

        Args:
            event: The event.

        Returns:
            The observers, in order of registration.
        """
        observers = self.observers_by_event_type.get(type(event))
        if not observers:
            return []
        return [observer for observer in observers.values() if observer.observes(event)]

    def observes(self, event_type: type, model: type[Model] | None = None) -> bool:
        """Whether some observer gets events of ``event_type`` - of ``model``, when given.

        Args:
            event_type: The event type.
            model: The model of the events.

        Returns:
            True when such an observer exists.
        """
        observers = self.observers_by_event_type.get(event_type)
        if not observers:
            return False
        if model is None:
            return True
        return any(observer.observes_model(model) for observer in observers.values())

    @staticmethod
    def get_model_set(event_type: type, models: Iterable[type[Model]] | None) -> frozenset[type[Model]] | None:
        """The models an observer is narrowed to.

        Args:
            event_type: The event type.
            models: The models, None for every event.

        Returns:
            The models, None for every event.

        Raises:
            QueryError: ``event_type`` isn't an event narrowed by model.
            TypeError: A model isn't a model class.
        """
        if models is None:
            return None
        model_set = frozenset(models)
        if not getattr(event_type, "observed_by_model", False):
            raise QueryError(f"{event_type.__name__} observers can't be narrowed to models")
        # Deferred: the models import the instrumentation package.
        from hare.models import Model

        for model in model_set:
            if not (isinstance(model, type) and issubclass(model, Model)):
                raise TypeError(f"Observers are narrowed to model classes, got {model!r}")
        return model_set
