from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, TypeVar

from hare.contrib.pydantic_field_aliases import PydanticFieldAliases

if TYPE_CHECKING:
    from hare.contrib.request_query.request_query import RequestQuery


TRequestQuery = TypeVar("TRequestQuery", bound="RequestQuery[Any]")


class RequestQueryInputs:
    """Collects repeated URL parameters and aliases before the request schema validates them."""

    @staticmethod
    def from_query_parameters(
        query_class: type[TRequestQuery],
        parameters: Iterable[tuple[str, str]],
        *,
        request: Any = None,
        **values: Any,
    ) -> TRequestQuery:
        """Builds the query from query parameters as pairs, a name repeated for each of its values.
        A parameter taking a list gets every value, any other the last one; a name the query
        doesn't have is ignored, as a web framework ignores it.
        Aliases sharing an input root receive the same value. The root keeps all repeated
        values when any of its fields needs a list or an indexed alias path; Pydantic checks
        whether that shared representation satisfies each field.
        An explicit field value removes that field's URL alias roots for every consumer.
        Other fields sharing those roots need another accepted input or a default.

        Args:
            parameters: ``(name, value)`` pairs.
            request: The request, if any.
            values: Values of other parameters - they win over ``parameters``.

        Returns:
            The query.

        Raises:
            InvalidRequestQuery: See ``__init__()``.
        """
        schema = query_class.prepare_parameters()
        collected: dict[str, list[str]] = {}
        for name, value in parameters:
            collected.setdefault(name, []).append(value)
        many_value_names = schema.get_many_value_parameter_names()
        paths_by_field = {
            name: PydanticFieldAliases.get_paths(name, field_info, schema.model_config)
            for name, field_info in schema.model_fields.items()
        }
        overridden_roots = {
            path[0]
            for name, paths in paths_by_field.items()
            if name in values or any(path[0] in values for path in paths)
            for path in paths
        }
        input_shapes: dict[str, bool] = {}
        for name, input_paths in paths_by_field.items():
            for path in input_paths:
                input_name: str = path[0]  # type: ignore[assignment]
                if input_name in overridden_roots or input_name not in collected:
                    continue
                takes_many = name in many_value_names or (len(path) > 1 and isinstance(path[1], int))
                input_shapes[input_name] = input_shapes.get(input_name, False) or takes_many
        arguments: dict[str, Any] = {
            name: collected[name] if takes_many else collected[name][-1] for name, takes_many in input_shapes.items()
        }
        arguments.update(values)
        return schema(request=request, **arguments)
