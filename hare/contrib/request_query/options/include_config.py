from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import Field

from hare.contrib.request_query.constants import (
    DEFAULT_INCLUDE_PARAMETER,
)
from hare.contrib.request_query.enums import ParameterType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset import QuerySet
from hare.contrib.request_query.options.parameter_field import ParameterField
from hare.contrib.request_query.options.request_option import RequestOption


@dataclasses.dataclass(frozen=True, slots=True)
class IncludeConfig(RequestOption):
    """The relations a request may ask to load with the rows: ``?include=author,tags`` loads a
    relation to one row with a join (``select_related()``) and a relation to many rows with a
    query of its own (``prefetch_related()``).

    Args:
        relations: The relation paths a request may name (``author``, ``author__profile``,
            ``tags``).
        parameter: The name of the parameter.
    """

    relations: tuple[str, ...]
    parameter: str = DEFAULT_INCLUDE_PARAMETER

    def __post_init__(self) -> None:
        self.check_allowed_names(type(self).__name__, self.relations)
        self.check_parameter_name(type(self).__name__, self.parameter)

    def get_parameter_fields(self) -> tuple[ParameterField, ...]:
        description = f"Relations to load with the rows, comma-separated: {', '.join(self.relations)}"
        return (
            ParameterField(
                self.parameter,
                str | None,
                Field(default=None, description=description),
                ParameterType.INCLUDE,
                self.relations,
            ),
        )

    def check_request(self, values: Mapping[str, Any]) -> None:
        for name in self.split_names(values.get(self.parameter)):
            if name not in self.relations:
                raise self.refuse(
                    self.parameter, f"Unknown name {name!r} - allowed: {', '.join(self.relations)}", "include"
                )

    def apply(self, queryset: QuerySet[Any], values: Mapping[str, Any], to_many: Mapping[str, bool]) -> QuerySet[Any]:
        """Loads the relations a request names.

        Args:
            queryset: The queryset of the rows.
            values: The request query's values by parameter.
            to_many: For each relation, whether it holds many rows.

        Returns:
            The queryset.
        """
        for path in self.split_names(values.get(self.parameter)):
            queryset = queryset.prefetch_related(path) if to_many[path] else queryset.select_related(path)
        return queryset
