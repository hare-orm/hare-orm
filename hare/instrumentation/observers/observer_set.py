from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import QueryError
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.instrumentation.observers.observer import Observer, ObserverCallback

if TYPE_CHECKING:
    from hare.core.apps.apps import Apps
    from hare.core.apps.declarations import ModelReference
    from hare.models import Model


class ObserverSet:
    """The observers of one scope - the process (``Observers``) or one ``HareContext`` - by event
    type, each event type's in order of registration."""

    #: How many observers every set holds together - with none anywhere, an event costs nothing.
    total_count: ClassVar[int] = 0

    def __init__(self) -> None:
        self.observers_by_event_type: dict[type, dict[ObserverCallback, Observer]] = {}

    def add(self, event_type: type, callback: ObserverCallback, models: Iterable[ModelReference] | None) -> None:
        """Adds an observer. Adding a callback again adds the models to the ones it observes; an
        observer of every event stays one, and adding it without models makes it one.

        Args:
            event_type: The event type.
            callback: The callback.
            models: The models whose events it gets - classes, ``"app.Model"`` labels or
                ``swappable()`` settings; None for every event.

        Raises:
            QueryError: ``event_type`` isn't an event narrowed by model.
            ConfigurationError: A label names no model of the current context, when one is set up.
        """
        observed = ObserverSet.get_observed_models(event_type, models)
        observers = self.observers_by_event_type.setdefault(event_type, {})
        existing = observers.get(callback)
        if existing is None:
            observers[callback] = (
                Observer(callback, None) if observed is None else Observer(callback, observed[0], observed[1])
            )
            ObserverSet.total_count += 1
        elif existing.models is not None:
            if observed is None:
                existing.models, existing.model_labels = None, frozenset()
            else:
                existing.models |= observed[0]
                existing.model_labels |= observed[1]

    def remove(self, event_type: type, callback: ObserverCallback, models: Iterable[ModelReference] | None) -> None:
        """Stops a callback observing the events of ``models`` - removes it entirely without them,
        or once it observes no model; nothing when it doesn't observe the event type.

        Args:
            event_type: The event type.
            callback: The callback.
            models: The models it stops observing - classes, ``"app.Model"`` labels or ``swappable()``
                settings; None to remove it.

        Raises:
            QueryError: The callback observes every event - it can only be removed entirely.
        """
        observed = ObserverSet.get_observed_models(event_type, models)
        observers = self.observers_by_event_type.get(event_type, {})
        existing = observers.get(callback)
        if existing is None:
            return
        if observed is not None:
            if existing.models is None:
                raise QueryError(f"{callback!r} observes every event - unobserve it without models to remove it")
            existing.models -= observed[0]
            existing.model_labels -= observed[1]
            if existing.models or existing.model_labels:
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
    def get_observed_models(
        event_type: type, models: Iterable[ModelReference] | None
    ) -> tuple[frozenset[type[Model]], frozenset[str]] | None:
        """The models an observer is narrowed to - its classes apart from its labels. A ``swappable()``
        setting is the label it points at in the current context; a label is looked up in the current
        context when one is set up, else when a context binds its models.

        Args:
            event_type: The event type.
            models: The models, None for every event.

        Returns:
            The classes and the labels, None for every event.

        Raises:
            QueryError: ``event_type`` isn't an event narrowed by model.
            ConfigurationError: A label names no model of the current context.
        """
        if models is None:
            return None
        if not getattr(event_type, "observed_by_model", False):
            raise QueryError(f"{event_type.__name__} observers can't be narrowed to models")
        # Local import: the context imports the observers.
        from hare.core.hare_context import HareContext

        context = HareContext.get_current()
        apps = context.apps if context is not None else None
        classes: set[type[Model]] = set()
        labels: set[str] = set()
        for reference in models:
            if isinstance(reference, type):
                classes.add(reference)
                continue
            if isinstance(reference, SwappableModelReference):
                reference = HareContext.require_current().swappable_label(reference.setting)
            if apps is not None:
                apps.get_model(reference)
            labels.add(reference)
        return frozenset(classes), frozenset(labels)

    def find_model_labels(self, apps: Apps) -> None:
        """Looks up the labels the observers name in a registry binding its models.

        Args:
            apps: The registry.

        Raises:
            ConfigurationError: A label names no model of the registry.
        """
        for observers in self.observers_by_event_type.values():
            for observer in observers.values():
                for label in observer.model_labels:
                    apps.get_model(label)
