from __future__ import annotations

import dataclasses
from enum import Enum
from typing import TYPE_CHECKING, Any, get_args, get_origin

from hare.contrib.request_query.bounds.lookup_pair_bounds import LookupPairBounds
from hare.contrib.request_query.bounds.range_bounds import RangeBounds
from hare.contrib.request_query.constants import LOOKUP_SEPARATOR
from hare.contrib.request_query.descriptions.constants import BACKWARD_RELATION_TYPES, TO_MANY_RELATION_TYPES
from hare.contrib.request_query.enums import BoundSide, ParameterType
from hare.contrib.request_query.options.in_path import InPath
from hare.contrib.request_query.value_annotation import ValueAnnotation
from hare.fields.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from pydantic.fields import FieldInfo

    from hare.contrib.request_query.declaration.filter_declaration import FilterDeclaration
    from hare.contrib.request_query.declaration.request_query_declaration import RequestQueryDeclaration
    from hare.contrib.request_query.options.parameter_field import ParameterField
    from hare.contrib.request_query.request_query import RequestQuery
    from hare.query.lookup_info.lookup_info import LookupInfo
from hare.contrib.request_query.descriptions.parameter_choice import ParameterChoice
from hare.contrib.request_query.descriptions.parameter_description import ParameterDescription
from hare.contrib.request_query.descriptions.relation_description import RelationDescription


class ParameterDescriber:
    """Describes every parameter of one request query class from its checked declaration."""

    def __init__(self, request_query_class: type[RequestQuery[Any]]) -> None:
        self.request_query_class = request_query_class

    def describe(self) -> tuple[ParameterDescription, ...]:
        """Describes the class's parameters, in their order.

        Returns:
            One description per parameter.

        Raises:
            ConfigurationError: The class's declaration is wrong - see
                ``RequestQuery.get_declaration()``.
        """
        declaration = self.request_query_class.get_declaration()
        option_fields = {
            parameter_field.name: parameter_field
            for option in self.request_query_class.get_request_options()
            for parameter_field in option.get_parameter_fields()
        }
        filters = {filter_declaration.parameter: filter_declaration for filter_declaration in declaration.filters}
        bounds = self.get_bounds(declaration)
        descriptions = []
        for name, field_info in self.request_query_class.model_fields.items():
            if name in option_fields:
                descriptions.append(self.describe_option(name, field_info, option_fields[name]))
            elif name in filters:
                descriptions.append(self.describe_filter(name, field_info, filters[name], bounds))
            else:
                descriptions.append(self.describe_common(name, field_info, ParameterType.NO_FILTER))
        return tuple(descriptions)

    def describe_common(self, name: str, field_info: FieldInfo, parameter_type: ParameterType) -> ParameterDescription:
        """What every parameter has.

        Args:
            name: The parameter.
            field_info: Its pydantic field.
            parameter_type: What it does.

        Returns:
            The description, without what only a filter or an option has.
        """
        required = field_info.is_required()
        return ParameterDescription(
            name=name,
            parameter_type=parameter_type,
            annotation=field_info.annotation,
            description=field_info.description,
            default=None if required else field_info.get_default(call_default_factory=True, validated_data={}),
            required=required,
            takes_many_values=ValueAnnotation.takes_many_values(field_info.annotation, field_info.metadata),
            in_path=any(isinstance(item, InPath) for item in field_info.metadata),
            choices=self.get_choices(field_info.annotation),
        )

    def describe_option(
        self, name: str, field_info: FieldInfo, parameter_field: ParameterField
    ) -> ParameterDescription:
        """Describes a parameter an option adds.

        Args:
            name: The parameter.
            field_info: Its pydantic field.
            parameter_field: What the option declares of it.

        Returns:
            The description.
        """
        return dataclasses.replace(
            self.describe_common(name, field_info, parameter_field.parameter_type),
            allowed_values=parameter_field.allowed_values,
        )

    def describe_filter(
        self,
        name: str,
        field_info: FieldInfo,
        filter_declaration: FilterDeclaration,
        bounds: dict[str, tuple[BoundSide, tuple[str, ...]]],
    ) -> ParameterDescription:
        """Describes a filter parameter - a filter method's has no filter key.

        Args:
            name: The parameter.
            field_info: Its pydantic field.
            filter_declaration: Its filter.
            bounds: The bound each bound parameter gives, with the parameters it pairs with.

        Returns:
            The description.
        """
        lookup_info = filter_declaration.lookup_info
        if lookup_info is None or filter_declaration.filter_key is None:
            return self.describe_common(name, field_info, ParameterType.FILTER_METHOD)
        bound, paired_parameters = bounds.get(name, (None, ()))
        return dataclasses.replace(
            self.describe_common(name, field_info, ParameterType.FILTER),
            filter_key=filter_declaration.filter_key,
            path=self.get_path(filter_declaration.filter_key, lookup_info),
            lookup=lookup_info.lookup,
            value_shape=lookup_info.value_shape,
            value_type=lookup_info.value_type,
            nullable=self.is_nullable(lookup_info),
            relation=self.describe_relation(lookup_info),
            bound=bound,
            paired_parameters=paired_parameters,
        )

    @staticmethod
    def get_path(filter_key: str, lookup_info: LookupInfo) -> str:
        """A filter key without its lookup.

        Args:
            filter_key: The key.
            lookup_info: The ORM's description of it.

        Returns:
            The key itself for equality, which has no suffix.
        """
        lookup = str(lookup_info.lookup)
        if not lookup:
            return filter_key
        return filter_key.removesuffix(f"{LOOKUP_SEPARATOR}{lookup}")

    @staticmethod
    def get_bounds(declaration: RequestQueryDeclaration) -> dict[str, tuple[BoundSide, tuple[str, ...]]]:
        """The bound each bound parameter of a declaration gives.

        Args:
            declaration: The declaration.

        Returns:
            Each bound parameter's side and the parameters giving the other bound of its path.
        """
        sides: dict[str, BoundSide] = {}
        pairs: dict[str, list[str]] = {}
        for parameter_bounds in declaration.bounds:
            if isinstance(parameter_bounds, RangeBounds):
                sides[parameter_bounds.parameter] = BoundSide.RANGE
                pairs.setdefault(parameter_bounds.parameter, [])
            elif isinstance(parameter_bounds, LookupPairBounds):
                sides[parameter_bounds.lower_parameter] = BoundSide.LOWER
                sides[parameter_bounds.upper_parameter] = BoundSide.UPPER
                pairs.setdefault(parameter_bounds.lower_parameter, []).append(parameter_bounds.upper_parameter)
                pairs.setdefault(parameter_bounds.upper_parameter, []).append(parameter_bounds.lower_parameter)
        return {parameter: (side, tuple(pairs.get(parameter, ()))) for parameter, side in sides.items()}

    @classmethod
    def is_nullable(cls, lookup_info: LookupInfo) -> bool:
        """Whether the value a filter compares can be missing.

        Args:
            lookup_info: The ORM's description of the filter.

        Returns:
            True for a nullable field (any field of a composite key), and for a path through a
            nullable forward relation or a relation a row may have no related row of.
        """
        compared = lookup_info.field if isinstance(lookup_info.field, tuple) else (lookup_info.field,)
        if any(isinstance(field, Field) and field.null for field in compared):
            return True
        for relation in lookup_info.relations:
            if relation.relation_type in BACKWARD_RELATION_TYPES or relation.null:
                return True
        return False

    @classmethod
    def describe_relation(cls, lookup_info: LookupInfo) -> RelationDescription | None:
        """The last relation a filter crosses.

        Args:
            lookup_info: The ORM's description of the filter.

        Returns:
            The relation, None for a filter of the model's own fields.
        """
        if not lookup_info.relations:
            return None
        relation = lookup_info.relations[-1]
        related_model = relation.related_model  # type: ignore[attr-defined]
        related_meta = related_model._meta
        key_fields = related_meta.pk_fields if related_meta.has_composite_primary_key else (related_meta.pk,)
        compared = lookup_info.field if isinstance(lookup_info.field, tuple) else (lookup_info.field,)
        return RelationDescription(
            path=LOOKUP_SEPARATOR.join(item.model_field_name for item in lookup_info.relations),
            model=related_model,
            key_fields=tuple(related_meta.primary_key_attribute_names),
            to_many=relation.relation_type in TO_MANY_RELATION_TYPES,
            compares_key=related_meta.has_primary_key and tuple(compared) == tuple(key_fields),
        )

    @classmethod
    def get_choices(cls, annotation: Any) -> tuple[ParameterChoice, ...] | None:
        """The members of the enum a parameter takes - itself, or the item of its list.

        Args:
            annotation: The parameter's annotation.

        Returns:
            Each member's value and name, None when the parameter takes no enum.
        """
        for alternative in ValueAnnotation.get_alternatives(annotation):
            candidates = [alternative]
            if get_origin(alternative) in (list, tuple, set, frozenset):
                candidates = [ValueAnnotation.strip(argument) for argument in get_args(alternative)]
            for candidate in candidates:
                if isinstance(candidate, type) and issubclass(candidate, Enum):
                    return tuple(ParameterChoice(value=member.value, label=member.name) for member in candidate)
        return None
