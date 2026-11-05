from __future__ import annotations

from typing import Any

from hare.migrations.constants import FIRST_MIGRATION


class SwappableDependency(tuple[str, str]):
    """A migration dependency on the first migration of the app a ``swappable`` config setting
    currently points into - ``(app_label, "__first__")``, remembering the setting it came from.

    Evaluated when the migration file is imported, so Hare must be initialized first.
    """

    setting: str

    def __new__(cls, setting: str) -> SwappableDependency:
        """
        Args:
            setting: The ``swappable`` config setting name, e.g. ``"USER_MODEL"``.

        Raises:
            ConfigurationError: Hare isn't initialized, or the setting is unknown.
        """
        from hare.fields.relations.swappable_model_reference import SwappableModelReference

        app_label = SwappableModelReference(setting).get_label().split(".", 1)[0]
        return cls.from_app_label(setting, app_label)

    @classmethod
    def from_app_label(cls, setting: str, app_label: str) -> SwappableDependency:
        """Build a dependency from an already selected app, without reading configuration."""
        dependency = super().__new__(cls, (app_label, FIRST_MIGRATION))
        dependency.setting = setting
        return dependency

    def __reduce__(self) -> tuple[Any, ...]:
        """Keep the selected app when copying or serializing migration definitions."""
        return type(self).from_app_label, (self.setting, self[0]), vars(self)


#: ``migrations.swappable_dependency("USER_MODEL")`` - a dependency on the app a ``swappable``
#: config setting points into.
swappable_dependency = SwappableDependency
