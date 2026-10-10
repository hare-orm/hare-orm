from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import ConfigurationError, ValidationError
from hare.fields import Field
from hare.fields.registrations.registered_lookup import RegisteredLookup
from hare.numbers.finite_numbers import FiniteNumbers
from hare.query.filters.lookups.field_lookup import FieldLookup
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper
from hare.vectors.constants import VECTOR_ELEMENT_MAX, VECTOR_MAX_DIMENSIONS, VECTOR_SEARCH_REQUIRED_FEATURE
from hare.vectors.enums import VectorDistanceType
from hare.vectors.terms.vector_distance_term import VectorDistanceTerm
from hare.vectors.terms.vector_literal import VectorLiteral

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model


class VectorField(Field[list[float]]):
    """A vector column - a fixed-length list of floats, for similarity search with
    ``L2Distance``/``CosineDistance``/``InnerProduct`` and the ``nearby`` lookup. Each dialect
    stores and compares it its own way; the distances need ``features.supports_vector_search``. A
    dialect needing an extension for it gets one from the migration autodetector wherever the field
    is used.

    Args:
        dimensions: The vector length - from 1 to 16000; a dialect may take fewer.

    Raises:
        ConfigurationError: ``dimensions`` isn't an int in its range.
    """

    COLUMN_TYPE_FROM_DIALECT = True

    field_type = list

    def __init__(self, dimensions: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if type(dimensions) is not int or not 1 <= dimensions <= VECTOR_MAX_DIMENSIONS:
            raise ConfigurationError(
                f"VectorField dimensions must be an int from 1 to {VECTOR_MAX_DIMENSIONS}, got {dimensions!r}"
            )
        self.dimensions = dimensions

    def get_python_type(self) -> Any:
        # `list[float]`, not the bare `list` of field_type - what a pydantic model generated from
        # the field accepts.
        return list[float]

    def validate(self, value: Any) -> None:
        super().validate(value)
        if value is None:
            return
        if not isinstance(value, (list, tuple)):
            raise ValidationError(
                f"{self.model_field_name}: expected a list/tuple of floats, got {self.get_value_for_message(value)}"
            )
        if len(value) != self.dimensions:
            raise ValidationError(
                f"{self.model_field_name}: expected a vector of {self.dimensions} dimension(s), got {len(value)}"
            )
        for index, element in enumerate(value):
            shown_element = self.get_value_for_message(element)
            if isinstance(element, (str, bytes, bool)):
                raise ValidationError(f"{self.model_field_name}[{index}]: expected a number, got {shown_element}")
            validation_error = None
            try:
                element_float = float(element)
            except (TypeError, ValueError) as error:
                validation_error = self.get_validation_error(
                    error, element, f"{self.model_field_name}[{index}]: expected a number, got {shown_element}"
                )
            if validation_error is not None:
                raise validation_error
            if not math.isfinite(element_float):
                raise ValidationError(f"{self.model_field_name}[{index}]: {shown_element} is not a finite number")
            if abs(element_float) > VECTOR_ELEMENT_MAX:
                raise ValidationError(
                    f"{self.model_field_name}[{index}]: {shown_element} is out of range for a vector element (float32)"
                )

    def to_db_value(self, value: Sequence[float] | None, instance: type[Model] | Model) -> list[float] | None:
        self.validate(value)
        if value is None:
            return None
        return [float(element) for element in value]

    def to_python(self, value: Any) -> list[float] | None:
        if value is None or isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        raise ValidationError(
            f"{self.model_field_name}: expected a list of floats, got {self.get_value_for_message(value)}"
        )

    @staticmethod
    def encode_nearby_value(
        value: tuple[Sequence[float], float], model: type[Model] | Model, field: Field[Any], dialect: Dialect
    ) -> tuple[Any, float]:
        """Converts ``(query_vector, max_distance)`` - the vector the way the dialect stores the field.

        Raises:
            ValidationError: The value isn't a pair, the vector doesn't fit the field, or the
                distance isn't a finite number.
        """
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValidationError(
                f"{field.model_field_name}__nearby: expected (query_vector, max_distance), got {value!r}"
            )
        query_vector, max_distance = value
        if not FiniteNumbers.is_finite_number(max_distance):
            raise ValidationError(
                f"{field.model_field_name}__nearby: the distance must be a finite number, got {max_distance!r}"
            )
        return dialect.types.get_db_value(field, query_vector, model), float(max_distance)

    @staticmethod
    def get_nearby_criterion(term: Term, value: tuple[Any, float]) -> Term:
        """The L2 distance of the column from the query vector, at most ``max_distance``."""
        vector_value, max_distance = value
        return VectorDistanceTerm(VectorDistanceType.L2, term, VectorLiteral(ValueWrapper(vector_value))).lte(
            max_distance
        )

    @staticmethod
    def get_nearby_lookup(field: Field[Any] | None) -> FieldLookup:
        """``.filter(embedding__nearby=(query_vector, max_distance))`` - the rows within an L2
        distance of the vector."""
        return FieldLookup(
            VectorField.get_nearby_criterion,
            VectorField.encode_nearby_value,
            required_feature=VECTOR_SEARCH_REQUIRED_FEATURE,
        )

    #: The field's own lookup, declared with the class - no query is built before it exists.
    registered_lookups: ClassVar[dict[str, RegisteredLookup]] = {
        "nearby": RegisteredLookup(builder=get_nearby_lookup, value_type=tuple)
    }
