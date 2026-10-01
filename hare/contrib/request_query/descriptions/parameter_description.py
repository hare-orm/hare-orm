from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from hare.contrib.request_query.enums import BoundSide, ParameterType
from hare.query.enums import Lookup, LookupValueShape

if TYPE_CHECKING:  # pragma: nocoverage
    pass
from hare.contrib.request_query.descriptions.parameter_choice import ParameterChoice
from hare.contrib.request_query.descriptions.relation_description import RelationDescription


@dataclasses.dataclass(frozen=True, slots=True)
class ParameterDescription:
    """One parameter of a request query.

    Attributes:
        name: The parameter's name.
        parameter_type: What it does.
        annotation: The type the request query validates its value with.
        description: Its description in the API schema, None for none.
        default: Its value when a request gives none (None for a required parameter).
        required: Whether a request must give it.
        takes_many_values: Whether it takes every value of a repeated query parameter, or a
            comma-separated list (``CommaSeparated``) - a multi-select.
        in_path: Whether it is read from the route's path (``InPath()``).
        choices: The values it may take with their labels - the members of the enum it takes -
            None when it takes any value of its type.
        allowed_values: The names an option's parameter may be given - the orderings a request
            may order by, the fields it may load, the relations it may include, the deleted-rows
            modes - None for any other parameter.
        filter_key: The ``.filter()`` key of a filter, None for any other parameter.
        path: ``filter_key`` without its lookup (``published_at`` of ``published_at__gte``).
        lookup: The lookup of a filter - a ``Lookup``, or a ``register_lookup()`` lookup's name.
        value_shape: Whether a filter takes one value, a list or a two-item range.
        value_type: The type of a filter's value - of each item of a list or range; a tuple of
            types for a composite key.
        nullable: Whether the value a filter compares can be missing - a nullable field, or a
            path through a nullable or to-many relation - so an "empty" choice
            (``<path>__isnull``) means something.
        relation: The relation a filter goes through, None for a filter of the model's own
            fields.
        bound: Which bound of its path a filter gives - lower, upper, or both as a range - None
            for a filter that isn't a bound.
        paired_parameters: The parameters giving the other bound of the same path - each upper
            bound of a lower one, each lower bound of an upper one.
    """

    name: str
    parameter_type: ParameterType
    annotation: Any
    description: str | None
    default: Any
    required: bool
    takes_many_values: bool
    in_path: bool
    choices: tuple[ParameterChoice, ...] | None = None
    allowed_values: tuple[str, ...] | None = None
    filter_key: str | None = None
    path: str | None = None
    lookup: Lookup | str | None = None
    value_shape: LookupValueShape | None = None
    value_type: Any = None
    nullable: bool = False
    relation: RelationDescription | None = None
    bound: BoundSide | None = None
    paired_parameters: tuple[str, ...] = ()
