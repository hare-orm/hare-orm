from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SwappableModelReference:
    """A relation target named by a ``swappable`` config setting instead of a fixed model - the
    model the setting points at, else the model declaring ``Meta.swappable`` with that name.

    A field keeps the reference itself, so a migration file stores the setting, not the model
    it currently points at.
    """

    setting: str

    def get_label(self) -> str:
        """The ``app_label.ModelName`` label of the model the setting currently points at.

        Raises:
            ConfigurationError: Hare isn't initialized, or the setting is neither configured
                nor declared by any model's ``Meta.swappable``.
        """
        from hare.core.hare import Hare

        return Hare.swappable_label(self.setting)

    def __repr__(self) -> str:
        return f"swappable({self.setting!r})"


#: ``swappable("USER_MODEL")`` - a relation target named by a ``swappable`` config setting.
swappable = SwappableModelReference
