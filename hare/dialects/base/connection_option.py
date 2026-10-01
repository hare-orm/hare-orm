from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from hare.dialects.base.constants import FALSE_STRINGS, TRUE_STRINGS
from hare.dialects.enums import ConnectionOptionType
from hare.exceptions import ConfigurationError


class ConnectionOption:
    """One setting a connection takes - its name, the type of value and the values allowed.

    A value may come typed from a configuration dict or as text from a DB_URL query parameter;
    both are checked the same way.

    Args:
        name: The setting's name, as configured.
        value_type: The type of value.
        minimum: The smallest number allowed.
        maximum: The largest number allowed.
        positive: A number of seconds must be greater than 0, not just at least 0.
        power_of_two: A whole number must be a power of two.
        choices: The allowed values of a choice, upper-cased.
    """

    __slots__ = ("name", "value_type", "minimum", "maximum", "positive", "power_of_two", "choices")

    def __init__(
        self,
        name: str,
        value_type: ConnectionOptionType,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
        positive: bool = False,
        power_of_two: bool = False,
        choices: Iterable[str] = (),
    ) -> None:
        self.name = name
        self.value_type = value_type
        self.minimum = minimum
        self.maximum = maximum
        self.positive = positive
        self.power_of_two = power_of_two
        self.choices = frozenset(choices)

    def parse(self, raw_value: Any) -> Any:
        """The checked value of the setting.

        Args:
            raw_value: The configured value, or its text from a DB_URL.

        Returns:
            An int, a float of seconds, a bool, an upper-cased choice or the text.

        Raises:
            ConfigurationError: The value isn't of the setting's type or is out of its range.
        """
        if self.value_type is ConnectionOptionType.WHOLE_NUMBER:
            return self._parse_whole_number(raw_value)
        if self.value_type is ConnectionOptionType.SECONDS:
            return self._parse_seconds(raw_value)
        if self.value_type is ConnectionOptionType.BOOLEAN:
            return self._parse_boolean(raw_value)
        if self.value_type is ConnectionOptionType.CHOICE:
            return self._parse_choice(raw_value)
        if not isinstance(raw_value, str):
            raise ConfigurationError(f"{self.name} must be a string, got {raw_value!r}")
        return raw_value

    def _parse_whole_number(self, raw_value: Any) -> int:
        if isinstance(raw_value, bool) or not isinstance(raw_value, int | str):
            raise ConfigurationError(f"{self.name} must be a whole number, got {raw_value!r}")
        try:
            value = int(raw_value)
        except ValueError:
            raise ConfigurationError(f"{self.name} must be a whole number, got {raw_value!r}") from None
        if (self.minimum is not None and value < self.minimum) or (self.maximum is not None and value > self.maximum):
            raise ConfigurationError(
                f"{self.name} must be between {self.minimum:g} and {self.maximum:g}, got {raw_value!r}"
            )
        if self.power_of_two and value & (value - 1):
            raise ConfigurationError(f"{self.name} must be a power of two, got {raw_value!r}")
        return value

    def _parse_seconds(self, raw_value: Any) -> float:
        if isinstance(raw_value, bool) or not isinstance(raw_value, int | float | str):
            raise ConfigurationError(f"{self.name} must be a number of seconds, got {raw_value!r}")
        try:
            value = float(raw_value)
        except ValueError:
            raise ConfigurationError(f"{self.name} must be a number of seconds, got {raw_value!r}") from None
        minimum_is_met = value > 0 if self.positive else value >= 0
        if not math.isfinite(value) or not minimum_is_met or (self.maximum is not None and value > self.maximum):
            lower_bound = "greater than 0" if self.positive else "at least 0"
            raise ConfigurationError(
                f"{self.name} must be {lower_bound} and at most {self.maximum:g} seconds, got {raw_value!r}"
            )
        return value

    def _parse_boolean(self, raw_value: Any) -> bool:
        if isinstance(raw_value, bool):
            return raw_value
        normalized_value = str(raw_value).strip().lower() if isinstance(raw_value, str | int) else None
        if normalized_value in TRUE_STRINGS:
            return True
        if normalized_value in FALSE_STRINGS:
            return False
        raise ConfigurationError(f"{self.name} must be a boolean, got {raw_value!r}")

    def _parse_choice(self, raw_value: Any) -> str:
        value = str(raw_value).strip().upper() if isinstance(raw_value, str | int) else None
        if isinstance(raw_value, bool) or value not in self.choices:
            raise ConfigurationError(
                f"Invalid value {raw_value!r} for {self.name}: expected one of {sorted(self.choices)}"
            )
        return value

    def __repr__(self) -> str:
        return f"<ConnectionOption {self.name} {self.value_type}>"
