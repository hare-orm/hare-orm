from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hare.contrib.request_query.enums import ParameterType
from hare.contrib.request_query.options.constants import DEFAULT_INCLUDE_PARAMETER

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
        return self.get_name_list_parameter_fields(
            self.parameter, self.relations, "Relations to load with the rows", ParameterType.INCLUDE
        )

    def check_request(self, values: Mapping[str, Any]) -> None:
        self.check_names_allowed(values, self.parameter, self.relations, "include")

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
