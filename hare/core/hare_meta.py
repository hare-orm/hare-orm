from __future__ import annotations

from typing import Any

from hare.utils.class_property import classproperty


class HareMeta(type):
    """The metaclass of ``Hare`` - assigning one of its class properties (``Hare.apps = ...``)
    raises instead of silently replacing the property with a plain value."""

    def __setattr__(cls, name: str, value: Any) -> None:
        if name in cls.__dict__ and isinstance(cls.__dict__[name], classproperty):
            raise AttributeError(
                f"Cannot assign to Hare.{name} - it is read from the current HareContext. "
                "Change the context itself instead."
            )
        super().__setattr__(name, value)
