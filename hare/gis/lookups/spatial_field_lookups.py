from __future__ import annotations

import math
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from hare.exceptions import ValidationError
from hare.gis.constants import (
    DISTANCE_LOOKUP_COMPARISONS,
    GEOGRAPHY_REQUIRED_FEATURE,
    IS_VALID_LOOKUP,
    KEPT_GENERIC_GEOMETRY_LOOKUPS,
    RELATE_PATTERN,
    SPATIAL_LOOKUP_REQUIRED_FEATURE,
)
from hare.gis.enums import SpatialFunctionType, SpatialRelation
from hare.gis.terms.geometry_value import GeometryValue
from hare.gis.terms.spatial_function_term import SpatialFunctionTerm
from hare.gis.terms.spatial_relation_term import SpatialRelationTerm
from hare.sql.enums import Equality
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field
    from hare.gis.fields.geometry_field import GeometryField
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup
    from hare.sql.terms.criteria.criterion import Criterion


class SpatialFieldLookups:
    """The lookups of a geometry field: equality and ``isnull``, the spatial relations
    (``intersects``, ``contains``, ``within``, ...) taking a geometry, ``dwithin`` and the distance
    comparisons (``distance_lte``, ...) taking ``(geometry, distance)``, ``relate`` taking ``(geometry,
    pattern)`` and ``isvalid`` taking a bool."""

    @classmethod
    def get_lookups(cls, field: GeometryField) -> dict[str, FieldLookup]:
        """Builds every lookup of a geometry field.

        Args:
            field: The field.

        Returns:
            The lookups by suffix.
        """
        # Local import: the filters package imports the dialects package.
        from hare.query.filters import ValueEncoders
        from hare.query.filters.lookups.field_lookup import FieldLookup
        from hare.query.filters.lookups.field_lookups import FieldLookups

        generic = FieldLookups.get_generic(field)
        lookups = {name: generic[name] for name in KEPT_GENERIC_GEOMETRY_LOOKUPS}
        # A geography is measured on the ellipsoid - a connection needs more for it.
        required_feature = GEOGRAPHY_REQUIRED_FEATURE if field.geography else SPATIAL_LOOKUP_REQUIRED_FEATURE
        encoder: Callable[..., Any]
        for relation in SpatialRelation:
            if relation == SpatialRelation.RELATE:
                encoder = cls.encode_geometry_with_pattern
            elif relation == SpatialRelation.DWITHIN:
                encoder = cls.encode_geometry_with_distance
            else:
                encoder = cls.encode_geometry
            lookups[relation.value] = FieldLookup(
                partial(cls.get_relation_criterion, relation, field),
                encoder,
                required_feature=required_feature,
            )
        for lookup_name, comparison in DISTANCE_LOOKUP_COMPARISONS.items():
            lookups[lookup_name] = FieldLookup(
                partial(cls.get_distance_criterion, comparison, field),
                cls.encode_geometry_with_distance,
                required_feature=required_feature,
            )
        lookups[IS_VALID_LOOKUP] = FieldLookup(
            partial(cls.get_is_valid_criterion, field),
            ValueEncoders.encode_bool,
            required_feature=required_feature,
        )
        return lookups

    @staticmethod
    def get_geometry_value(value: Any, field: Field[Any]) -> GeometryValue:
        """A geometry from Python as the term it is compared through.

        Args:
            value: The geometry, in any form the field takes.
            field: The geometry field.

        Returns:
            The term - transformed to the field's SRID when the geometry has another.
        """
        geometry_field: GeometryField = field  # type: ignore[assignment]
        geometry = geometry_field.get_parsed_geometry(value)
        return GeometryValue(geometry, geometry_field.srid, geometry_field.geography)

    @staticmethod
    def encode_geometry(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> Term:
        """Encodes the geometry of a spatial relation."""
        return SpatialFieldLookups.get_geometry_value(value, field)

    @staticmethod
    def get_pair(value: Any, field: Field[Any], second_name: str) -> tuple[Any, Any]:
        """The two items of a ``(geometry, ...)`` lookup value.

        Raises:
            ValidationError: ``value`` isn't a pair.
        """
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise ValidationError(
                f"{field.model_field_name}: expected a (geometry, {second_name}) pair, "
                f"got {field.get_value_for_message(value)}"
            )
        return value[0], value[1]

    @staticmethod
    def encode_geometry_with_distance(
        value: Any, obj: Model, field: Field[Any], dialect: Dialect
    ) -> tuple[GeometryValue, ValueWrapper]:
        """Encodes ``(geometry, distance)`` - a distance is a finite number of zero or more, in meters
        on a geography and in the SRID's units on a geometry.

        Raises:
            ValidationError: ``value`` isn't such a pair.
        """
        geometry, distance = SpatialFieldLookups.get_pair(value, field, "distance")
        if isinstance(distance, bool) or not isinstance(distance, (int, float)) or not 0 <= distance < math.inf:
            raise ValidationError(
                f"{field.model_field_name}: a distance is a finite number of zero or more, got {distance!r}"
            )
        return SpatialFieldLookups.get_geometry_value(geometry, field), ValueWrapper(float(distance))

    @staticmethod
    def encode_geometry_with_pattern(
        value: Any, obj: Model, field: Field[Any], dialect: Dialect
    ) -> tuple[GeometryValue, ValueWrapper]:
        """Encodes ``(geometry, pattern)`` - a DE-9IM pattern of nine ``0``, ``1``, ``2``, ``T``,
        ``F``, ``*``.

        Raises:
            ValidationError: ``value`` isn't such a pair.
        """
        geometry, pattern = SpatialFieldLookups.get_pair(value, field, "pattern")
        if not isinstance(pattern, str) or RELATE_PATTERN.fullmatch(pattern) is None:
            raise ValidationError(
                f"{field.model_field_name}: a relate pattern is nine of 0, 1, 2, T, F, *, got {pattern!r}"
            )
        return SpatialFieldLookups.get_geometry_value(geometry, field), ValueWrapper(pattern.upper())

    @staticmethod
    def get_relation_criterion(
        relation: SpatialRelation, field: GeometryField, field_term: Term, value: Any
    ) -> Criterion:
        """The criterion of a spatial relation.

        Args:
            relation: The relation.
            field: The column's field.
            field_term: The column.
            value: The other geometry, or a pair of it and the relation's argument.

        Returns:
            The criterion.
        """
        extra_arguments = value[1:] if isinstance(value, tuple) else ()
        geometry_term = value[0] if isinstance(value, tuple) else value
        return SpatialRelationTerm(
            relation, field_term, geometry_term, *extra_arguments, geography=field.geography, field=field
        )

    @staticmethod
    def get_distance_criterion(comparison: Equality, field: GeometryField, field_term: Term, value: Any) -> Criterion:
        """The criterion comparing the distance from the column's geometry to another with a distance.

        Args:
            comparison: The comparison.
            field: The column's field.
            field_term: The column.
            value: The pair of the other geometry and the distance.

        Returns:
            The criterion.
        """
        geometry_term, distance_term = value
        distance = SpatialFunctionTerm(
            SpatialFunctionType.DISTANCE,
            field_term,
            geometry_term,
            geography=field.geography,
            geometry_argument_count=2,
            geometry_fields=(field, None),
        )
        return BasicCriterion(comparison, distance, distance_term)

    @staticmethod
    def get_is_valid_criterion(field: GeometryField, field_term: Term, value: Any) -> Criterion:
        """Whether the column's geometry is valid, compared with ``value``.

        Args:
            field: The column's field.
            field_term: The column.
            value: The encoded bool.

        Returns:
            The criterion.
        """
        is_valid = SpatialFunctionTerm(
            SpatialFunctionType.IS_VALID, field_term, geography=field.geography, geometry_fields=(field,)
        )
        return BasicCriterion(Equality.EQ, is_valid, value if isinstance(value, Term) else ValueWrapper(value))
