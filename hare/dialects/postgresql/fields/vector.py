from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.dialects.enums import DialectName
from hare.dialects.postgresql.constants import FLOAT4_MAX
from hare.dialects.postgresql.lookups.declarations import VectorInfixOperator
from hare.exceptions import ValidationError
from hare.fields import Field
from hare.query.filters.field_lookup import FieldLookup
from hare.sql.functions.cast import Cast
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect  # pragma: nocoverage
    from hare.models import Model


class VectorField(Field[list[float]]):
    """A pgvector ``vector(N)`` column - a fixed-length list of floats, for similarity search with
    ``L2Distance``/``CosineDistance``/``InnerProduct``. Needs the ``vector`` extension; the
    migration autodetector adds ``CreateExtension("vector")`` wherever the field is used.

    Args:
        dimensions: The vector length.
    """

    SUPPORTED_DIALECTS = frozenset({DialectName.POSTGRESQL})

    field_type = list
    requires_extension = "vector"

    def __init__(self, dimensions: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.dimensions = dimensions

    @property
    def SQL_TYPE(self) -> str:  # type: ignore[override]
        return f"vector({self.dimensions})"

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
            # pgvector stores finite float4 values only - NaN/infinity are rejected by the server.
            shown_element = self.get_value_for_message(element)
            if isinstance(element, (str, bytes, bool)):
                raise ValidationError(f"{self.model_field_name}[{index}]: expected a number, got {shown_element}")
            validation_error = None
            try:
                element_float = float(element)
            except (TypeError, ValueError) as exc:
                validation_error = self.get_validation_error(
                    exc, element, f"{self.model_field_name}[{index}]: expected a number, got {shown_element}"
                )
            if validation_error is not None:
                raise validation_error
            if not math.isfinite(element_float):
                raise ValidationError(f"{self.model_field_name}[{index}]: {shown_element} is not a finite number")
            if abs(element_float) > FLOAT4_MAX:
                raise ValidationError(
                    f"{self.model_field_name}[{index}]: {shown_element} is out of range for a vector element (float4)"
                )

    def to_db_value(self, value: Sequence[float] | None, instance: type[Model] | Model) -> str | None:
        self.validate(value)
        if value is None:
            return None
        return self.format_vector_text(value)

    def to_python(self, value: Any) -> list[float] | None:
        if value is None or isinstance(value, list):
            return value
        if isinstance(value, str):
            return self.parse_vector_text(value)
        if isinstance(value, tuple):
            return list(value)
        raise ValidationError(
            f"{self.model_field_name}: expected a list of floats, got {self.get_value_for_message(value)}"
        )

    @staticmethod
    def format_vector_text(values: Sequence[float]) -> str:
        """Formats a list of floats as pgvector's bracketed text representation."""
        return "[" + ",".join(repr(float(v)) for v in values) + "]"

    @staticmethod
    def parse_vector_text(raw: str) -> list[float]:
        """Parses a vector read as pgvector's text (``"[0.1,0.2,0.3]"``, what asyncpg returns), or as a
        hex-encoded binary payload.
        """
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            inner = stripped[1:-1]
            if not inner:
                return []
            return [float(element) for element in inner.split(",")]
        return VectorField._parse_binary_hex(stripped)

    @staticmethod
    def _parse_binary_hex(raw: str) -> list[float]:
        """Parses pgvector's binary send/recv wire format: a big-endian uint16 element count, a
        reserved uint16 (always 0), then that many big-endian float4 values."""
        data = bytes.fromhex(raw)
        dimensions = struct.unpack(">H", data[0:2])[0]
        return list(struct.unpack(f">{dimensions}f", data[4 : 4 + dimensions * 4]))

    @staticmethod
    def _nearby_lookup(field: Field[Any] | None) -> FieldLookup:
        """``.filter(embedding__nearby=(query_vector, max_distance))``. The lookup's encoder converts
        ``query_vector`` through the field, so a vector of a wrong length or content raises
        ``ValidationError``.
        """

        def _encode_nearby_value(
            value: tuple[Sequence[float], float],
            model: type[Model] | Model,
            field_object: Field[Any],
            dialect: Dialect,
        ) -> tuple[str, float]:
            query_vector, max_distance = value
            return field_object.to_db_value(query_vector, model), max_distance

        def _operator(term: Term, value: tuple[str, float]) -> Term:
            formatted_vector, max_distance = value
            literal = Cast(ValueWrapper(formatted_vector), "vector")
            distance = VectorInfixOperator(term, " <-> ", literal)
            return distance.lte(max_distance)

        return FieldLookup(_operator, _encode_nearby_value)


VectorField.register_lookup("nearby", VectorField._nearby_lookup, value_type=tuple)
