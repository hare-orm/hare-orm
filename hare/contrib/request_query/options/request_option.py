from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hare.contrib.request_query.constants import (
    NAME_SEPARATOR,
)
from hare.contrib.request_query.exceptions import InvalidRequestQuery
from hare.contrib.request_query.options.parameter_field import ParameterField
from hare.exceptions import ConfigurationError


class RequestOption:
    """An option of a request query's ``Meta`` that adds parameters: it names them and checks the
    values a request gives them."""

    __slots__ = ()

    def get_parameter_fields(self) -> tuple[ParameterField, ...]:
        """The parameters the option adds.

        Returns:
            Each parameter's name, type and default.
        """
        raise NotImplementedError

    def get_parameters(self) -> tuple[str, ...]:
        """The names of the parameters the option adds.

        Returns:
            The names.
        """
        return tuple(parameter_field.name for parameter_field in self.get_parameter_fields())

    def check_request(self, values: Mapping[str, Any]) -> None:
        """Checks the values a request gives the option's parameters - beyond their types, which
        pydantic checks. Nothing more to check by default.

        Args:
            values: The request query's values by parameter.

        Raises:
            InvalidRequestQuery: A value isn't allowed.
        """

    @staticmethod
    def split_names(value: str | None) -> tuple[str, ...]:
        """The names of a comma-separated parameter - an ordering, fields, relations.

        Args:
            value: The parameter - names joined with commas.

        Returns:
            The non-empty names, stripped.
        """
        if not value:
            return ()
        return tuple(name.strip() for name in value.split(NAME_SEPARATOR) if name.strip())

    @staticmethod
    def check_parameter_name(option_name: str, parameter: str) -> None:
        """Checks the name of an option's parameter.

        Args:
            option_name: The option's class name, for the error.
            parameter: The name.

        Raises:
            ConfigurationError: The name isn't an identifier.
        """
        if not isinstance(parameter, str) or not parameter.isidentifier():
            raise ConfigurationError(f"{option_name}.parameter must be an identifier, got {parameter!r}")

    @staticmethod
    def check_allowed_names(option_name: str, names: tuple[str, ...]) -> None:
        """Checks the names an option lets a request choose from.

        Args:
            option_name: The option's class name, for the error.
            names: The names.

        Raises:
            ConfigurationError: The names aren't a non-empty tuple of distinct names.
        """
        if not isinstance(names, tuple) or not names or not all(isinstance(name, str) and name for name in names):
            raise ConfigurationError(f"{option_name} needs a non-empty tuple of names, got {names!r}")
        if len(set(names)) != len(names):
            raise ConfigurationError(f"{option_name} names each name once, got {names!r}")

    @staticmethod
    def refuse(parameter: str, message: str, error_type: str) -> InvalidRequestQuery:
        """The error of a value a request gives one parameter.

        Args:
            parameter: The parameter.
            message: What is wrong.
            error_type: The type of error.

        Returns:
            The error to raise.
        """
        return InvalidRequestQuery([{"loc": [parameter], "msg": message, "type": error_type}])
