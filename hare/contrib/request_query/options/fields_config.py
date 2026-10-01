from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from pydantic import Field

from hare.contrib.request_query.constants import (
    DEFAULT_FIELDS_PARAMETER,
)
from hare.contrib.request_query.enums import ParameterType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset import QuerySet
from hare.contrib.request_query.options.parameter_field import ParameterField
from hare.contrib.request_query.options.request_option import RequestOption


@dataclasses.dataclass(frozen=True, slots=True)
class FieldsConfig(RequestOption):
    """The fields a request may ask to load: ``?fields=title,published_at`` loads only them
    (``QuerySet.only()``), the primary key and the fields the ordering reads - reading another
    field of such a row raises ``AttributeError``.

    Args:
        fields: The names a request may name - the model's own fields, not relations to follow.
        parameter: The name of the parameter.
    """

    fields: tuple[str, ...]
    parameter: str = DEFAULT_FIELDS_PARAMETER

    def __post_init__(self) -> None:
        self.check_allowed_names(type(self).__name__, self.fields)
        self.check_parameter_name(type(self).__name__, self.parameter)

    def get_parameter_fields(self) -> tuple[ParameterField, ...]:
        description = f"Fields to load, comma-separated: {', '.join(self.fields)}"
        return (
            ParameterField(
                self.parameter,
                str | None,
                Field(default=None, description=description),
                ParameterType.FIELDS,
                self.fields,
            ),
        )

    def check_request(self, values: Mapping[str, Any]) -> None:
        for name in self.split_names(values.get(self.parameter)):
            if name not in self.fields:
                raise self.refuse(
                    self.parameter, f"Unknown name {name!r} - allowed: {', '.join(self.fields)}", "fields"
                )

    def apply(self, queryset: QuerySet[Any], values: Mapping[str, Any], always_loaded: Iterable[str]) -> QuerySet[Any]:
        """Loads only the fields a request names.

        Args:
            queryset: The queryset of the rows.
            values: The request query's values by parameter.
            always_loaded: The fields loaded whatever the request names - the primary key, the
                fields the ordering reads.

        Returns:
            The queryset - unchanged when the request names no field.
        """
        names = self.split_names(values.get(self.parameter))
        if not names:
            return queryset
        return queryset.only(*dict.fromkeys([*always_loaded, *names]))
