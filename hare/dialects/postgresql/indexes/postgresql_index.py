from typing import Any

from hare.ddl.indexes.partial_index import PartialIndex
from hare.exceptions import ConfigurationError


class PostgresqlIndex(PartialIndex):
    def validate_storage_parameter(self, name: str, value: Any, value_range: tuple[int, int]) -> int:
        """Checks an integer ``WITH (...)`` storage parameter before it's written into the DDL.

        Args:
            name: The parameter's name.
            value: The given value.
            value_range: The inclusive minimum and maximum.

        Returns:
            The value.

        Raises:
            ConfigurationError: The value isn't an int (a bool isn't one) or is out of range.
        """
        minimum, maximum = value_range
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigurationError(f"{type(self).__name__} {name} must be an int, got {value!r}")
        if not minimum <= value <= maximum:
            raise ConfigurationError(
                f"{type(self).__name__} {name} must be between {minimum} and {maximum}, got {value}"
            )
        return value
