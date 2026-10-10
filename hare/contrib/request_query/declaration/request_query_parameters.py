from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic.fields import FieldInfo

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.request_query.request_query import RequestQuery


class RequestQueryParameters:
    """The parameters of a request query class: added or replaced in one order with the validation
    rebuilt, and their values on a request query - what its options read the request from."""

    @staticmethod
    def add_parameter_fields(
        query_class: type[RequestQuery[Any]], fields: Mapping[str, FieldInfo], *, raise_errors: bool = True
    ) -> None:
        """Adds or replaces parameters of the class and rebuilds its validation. The parameters
        keep one order: the ones the class declares and ``Meta.filters`` makes, then the options'.

        Args:
            query_class: The request query class.
            fields: The pydantic fields by parameter.
            raise_errors: Whether an annotation that can't be resolved yet raises - a class being
                created leaves it to pydantic to rebuild once it can.
        """
        if not fields:
            return
        option_parameters = {
            parameter for option in query_class.get_request_options() for parameter in option.get_parameters()
        }
        merged_fields = {**query_class.__pydantic_fields__, **fields}
        query_class.__pydantic_fields__.clear()
        query_class.__pydantic_fields__.update(
            {name: field for name, field in merged_fields.items() if name not in option_parameters}
        )
        query_class.__pydantic_fields__.update(
            {name: field for name, field in merged_fields.items() if name in option_parameters}
        )
        query_class.model_rebuild(force=True, raise_errors=raise_errors)

    @staticmethod
    def get_parameter_values(request_query: RequestQuery[Any]) -> Mapping[str, Any]:
        """The values of the query's parameters - what its options read the request from.

        Args:
            request_query: The request query.

        Returns:
            Each parameter's value, by name.
        """
        return {name: getattr(request_query, name) for name in type(request_query).model_fields}
