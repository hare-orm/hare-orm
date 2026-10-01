from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from pydantic import Field

from hare.contrib.request_query.constants import (
    DEFAULT_ORDERING_PARAMETER,
    DESCENDING_PREFIX,
)
from hare.contrib.request_query.enums import ParameterType
from hare.contrib.request_query.options.parameter_field import ParameterField
from hare.contrib.request_query.options.request_option import RequestOption
from hare.exceptions import ConfigurationError


@dataclasses.dataclass(frozen=True, slots=True)
class OrderingConfig(RequestOption):
    """The orderings a request may ask for, and the one it gets when it asks for none.

    A request names one or more of ``fields`` in its parameter, comma-separated, each with ``-``
    for descending: ``?ordering=-created_at,name``. Every ordering ends with the model's primary
    key, so rows with equal values keep one order from page to page.

    Args:
        fields: The names a request may order by - fields or paths through relations. Empty: a
            request can't choose, and the option only gives the default.
        default: The ordering of a request that names none, ``-`` for descending. Empty: the
            ordering of the query's queryset, else of the model's ``Meta.ordering``.
        parameter: The name of the parameter.
    """

    fields: tuple[str, ...] = ()
    default: tuple[str, ...] = ()
    parameter: str = DEFAULT_ORDERING_PARAMETER

    def __post_init__(self) -> None:
        for option_name, names in (("fields", self.fields), ("default", self.default)):
            if not isinstance(names, tuple) or not all(isinstance(name, str) and name for name in names):
                raise ConfigurationError(f"OrderingConfig.{option_name} must be a tuple of field names, got {names!r}")
        if any(name.startswith(DESCENDING_PREFIX) for name in self.fields):
            raise ConfigurationError(
                f"OrderingConfig.fields names fields without a direction, got {self.fields!r} - "
                "a request chooses the direction itself"
            )
        self.check_parameter_name(type(self).__name__, self.parameter)

    def get_parameter_fields(self) -> tuple[ParameterField, ...]:
        if not self.fields:
            return ()
        allowed = ", ".join(f"{name}, -{name}" for name in self.fields)
        description = f"Names to order by, comma-separated, - for descending: {allowed}"
        return (
            ParameterField(
                self.parameter,
                str | None,
                Field(default=None, description=description),
                ParameterType.ORDERING,
                self.fields,
            ),
        )

    def get_requested(self, values: Mapping[str, Any]) -> tuple[str, ...]:
        """The ordering a request names.

        Args:
            values: The request query's values by parameter.

        Returns:
            The names with their ``-``, empty when the request names none.
        """
        if not self.fields:
            return ()
        return self.split_names(values.get(self.parameter))

    def check_request(self, values: Mapping[str, Any]) -> None:
        bare_names = [name.removeprefix(DESCENDING_PREFIX) for name in self.get_requested(values)]
        for name in bare_names:
            if name not in self.fields:
                raise self.refuse(
                    self.parameter, f"Can't order by {name!r} - allowed: {', '.join(self.fields)}", "ordering"
                )
        if len(set(bare_names)) != len(bare_names):
            raise self.refuse(self.parameter, "A field is named more than once", "ordering")
