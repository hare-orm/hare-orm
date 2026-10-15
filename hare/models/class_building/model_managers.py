from __future__ import annotations

import inspect
from typing import Any

from hare.exceptions import ConfigurationError
from hare.models.enums import ModelOption
from hare.query.managers.manager import Manager


class ModelManagers:
    """The managers of a model class: the default one behind Model.objects - the objects its body
    declares, else Meta.manager, else a base model's objects - and a fresh copy of every manager a
    base declares."""

    @staticmethod
    def get_default_manager(
        name: str,
        attributes: dict[str, Any],
        declares_objects: bool,
        meta_class: type,
        meta_manager: Manager[Any],
    ) -> Manager[Any]:
        """The manager behind ``Model.objects`` - the model's default manager, which a JOIN to
        the model is scoped by: the ``objects`` the class body declares, else ``Meta.manager``,
        else the ``objects`` of a base model, else a plain ``Manager``.

        Args:
            name: The model's name.
            attributes: The attributes collected for the class - a base model's managers copied in.
            declares_objects: Whether the class body itself assigns ``objects``.
            meta_class: The model's ``Meta``, with its abstract ancestors' options merged in.
            meta_manager: The manager made from ``Meta.manager``, or a plain one.

        Returns:
            The manager.

        Raises:
            ConfigurationError: ``objects`` is assigned something that isn't a ``Manager``.
        """
        objects = attributes.get("objects")
        if declares_objects:
            if not isinstance(objects, Manager):
                raise ConfigurationError(f"{name}.objects must be a Manager, got {type(objects).__name__}")
            return objects
        # Read without running the manager's descriptor - off a class it gives a queryset.
        if inspect.getattr_static(meta_class, ModelOption.MANAGER, None) is None and isinstance(objects, Manager):
            return objects
        return meta_manager

    @staticmethod
    def copy_manager_attributes(base: type, attributes: dict[str, Any]) -> None:
        """Adds a fresh copy of every ``Manager`` attribute ``base`` itself declares to ``attrs``,
        unless ``attrs`` already holds that name.

        Args:
            base: The class whose own namespace is scanned.
            attributes: The attributes being collected for the class under construction.
        """
        for key, value in base.__dict__.items():
            if isinstance(value, Manager) and key not in attributes:
                attributes[key] = value.copy_unbound()
