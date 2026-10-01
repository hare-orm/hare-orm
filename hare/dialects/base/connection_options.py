from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from hare.dialects.base.connection_option import ConnectionOption
from hare.exceptions import ConfigurationError


class ConnectionOptions:
    """The settings a driver's connections take.

    Args:
        options: The settings.
    """

    __slots__ = ("by_name",)

    def __init__(self, *options: ConnectionOption) -> None:
        self.by_name: dict[str, ConnectionOption] = {option.name: option for option in options}

    def __add__(self, other: ConnectionOptions) -> ConnectionOptions:
        return ConnectionOptions(*self.by_name.values(), *other.by_name.values())

    def __contains__(self, name: object) -> bool:
        return name in self.by_name

    @property
    def names(self) -> list[str]:
        """The settings' names, sorted."""
        return sorted(self.by_name)

    def read(self, settings: dict[str, Any], defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Takes the known settings out of ``settings`` and checks them.

        Args:
            settings: The configured settings; the known ones are removed.
            defaults: Values of the settings left unset - used as given, unchecked.

        Returns:
            Every known setting that is set or has a default, checked.

        Raises:
            ConfigurationError: A value isn't allowed for its setting.
        """
        values: dict[str, Any] = dict(defaults or {})
        for name, option in self.by_name.items():
            if name in settings:
                raw_value = settings.pop(name)
                values[name] = None if raw_value is None else option.parse(raw_value)
        return values

    def raise_for_unknown(self, settings: Iterable[str], driver_name: str) -> None:
        """Rejects settings the driver doesn't take - silently dropped, a misspelled one would
        leave its default in force.

        Args:
            settings: The names of the settings left over.
            driver_name: Names the driver in the error.

        Raises:
            ConfigurationError: A setting isn't one of the driver's.
        """
        unknown_names = sorted(set(settings) - self.by_name.keys())
        if unknown_names:
            raise ConfigurationError(
                f"Unknown connection parameter(s) {unknown_names!r} for the {driver_name} driver: expected one of "
                f"{self.names}"
            )
