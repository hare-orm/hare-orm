from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from typing import Any

from hare.classes.value_descriptions import ValueDescriptions
from hare.dialects.base.constants import FALSE_STRINGS, TRUE_STRINGS
from hare.dialects.enums import ConnectionOptionType
from hare.exceptions import ConfigurationError


class ConnectionOption:
    """One connection setting and the conversion of its URL text.

    A value may come typed from a configuration dict or as text from a DB_URL query parameter;
    typed driver settings pass through unchanged. SQL values need validation because their
    rendered representation becomes part of a statement rather than a bound parameter.

    Args:
        name: The setting's name, as configured.
        value_type: The type of value.
        sql_value: Validate values that are rendered directly into SQL.
        minimum: The smallest SQL number allowed.
        maximum: The largest SQL number allowed.
        power_of_two: An SQL whole number must be a power of two.
        choices: The allowed values of a choice, upper-cased.
    """

    __slots__ = ("name", "value_type", "sql_value", "minimum", "maximum", "power_of_two", "choices")

    def __init__(
        self,
        name: str,
        value_type: ConnectionOptionType,
        *,
        sql_value: bool = False,
        minimum: float | None = None,
        maximum: float | None = None,
        power_of_two: bool = False,
        choices: Iterable[str] = (),
    ) -> None:
        self.name = name
        self.value_type = value_type
        self.sql_value = sql_value
        self.minimum = minimum
        self.maximum = maximum
        self.power_of_two = power_of_two
        self.choices = frozenset(choices)

    def parse(self, raw_value: Any) -> Any:
        """Convert URL text, declared choices and callbacks; validate direct SQL values.

        Args:
            raw_value: The configured value, or its text from a DB_URL.

        Returns:
            The converted text, or the supplied typed value for a driver setting.

        Raises:
            ConfigurationError: Text cannot be parsed, a choice/callback is invalid, or a SQL
                value violates its declared contract.
        """
        if self.value_type is ConnectionOptionType.CALLABLE:
            return self._parse_callable(raw_value)
        if self.value_type is ConnectionOptionType.CHOICE:
            return self._parse_choice(raw_value)
        if not self.sql_value and not isinstance(raw_value, str):
            return raw_value
        if self.value_type is ConnectionOptionType.WHOLE_NUMBER:
            return self._parse_whole_number(raw_value)
        if self.value_type is ConnectionOptionType.SECONDS:
            return self._parse_seconds(raw_value)
        if self.value_type is ConnectionOptionType.BOOLEAN:
            return self._parse_boolean(raw_value)
        if not isinstance(raw_value, str):
            raise ConfigurationError(f"{self.name} must be a string, got {raw_value!r}")
        return raw_value

    def _parse_whole_number(self, raw_value: Any) -> int:
        if self.sql_value and (isinstance(raw_value, bool) or not isinstance(raw_value, (int, str))):
            raise ConfigurationError(f"{self.name} must be a whole number, got {raw_value!r}")
        try:
            value = int(raw_value)
        except ValueError:
            raise ConfigurationError(f"{self.name} must be a whole number, got {raw_value!r}") from None
        if self.sql_value and (
            (self.minimum is not None and value < self.minimum) or (self.maximum is not None and value > self.maximum)
        ):
            raise ConfigurationError(
                f"{self.name} must be between {self.minimum:g} and {self.maximum:g}, got "
                f"{ValueDescriptions.get_shown_value(raw_value)}"
            )
        if self.sql_value and self.power_of_two and value & (value - 1):
            raise ConfigurationError(f"{self.name} must be a power of two, got {raw_value!r}")
        return value

    def _parse_seconds(self, raw_value: Any) -> float:
        try:
            return float(raw_value)
        except (ValueError, OverflowError):
            raise ConfigurationError(f"{self.name} must be a number of seconds, got {raw_value!r}") from None

    def _parse_boolean(self, raw_value: Any) -> bool:
        if isinstance(raw_value, bool):
            return raw_value
        normalized_value = str(raw_value).strip().lower() if isinstance(raw_value, (str, int)) else None
        if normalized_value in TRUE_STRINGS:
            return True
        if normalized_value in FALSE_STRINGS:
            return False
        raise ConfigurationError(f"{self.name} must be a boolean, got {raw_value!r}")

    def _parse_choice(self, raw_value: Any) -> str:
        value = str(raw_value).strip().upper()
        if value not in self.choices:
            raise ConfigurationError(
                f"Invalid value {raw_value!r} for {self.name}: expected one of {sorted(self.choices)}"
            )
        return value

    def _parse_callable(self, raw_value: Any) -> Callable[..., Any]:
        # A DB_URL or a config file names the function by its dotted path.
        if isinstance(raw_value, str):
            module_name, _, attribute_name = raw_value.rpartition(".")
            try:
                raw_value = getattr(importlib.import_module(module_name), attribute_name)
            except (ImportError, AttributeError, ValueError):
                raise ConfigurationError(
                    f"{self.name} must be a function or its dotted path, can't import {raw_value!r}"
                ) from None
        if not callable(raw_value):
            raise ConfigurationError(f"{self.name} must be a function or its dotted path, got {raw_value!r}")
        return raw_value

    def __repr__(self) -> str:
        return f"<ConnectionOption {self.name} {self.value_type}>"
