from __future__ import annotations

import struct
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.models import Model
    from hare.vectors.vector_field import VectorField


class PostgresqlVectorValues:
    """How PostgreSQL stores a ``VectorField`` - pgvector's ``vector(N)``, written and read as its
    bracketed text."""

    @staticmethod
    def get_column_type(field: Field[Any]) -> str:
        """``vector(N)``.

        Args:
            field: The vector field.

        Returns:
            The column type.
        """
        return f"vector({cast('VectorField', field).dimensions})"

    @staticmethod
    def to_db(field: Field[Any], value: Sequence[float] | None, instance: type[Model] | Model) -> str | None:
        """The vector as pgvector's text.

        Args:
            field: The vector field.
            value: The vector.
            instance: The instance or model written.

        Returns:
            The text, None for no vector.

        Raises:
            ValidationError: The value doesn't fit the field.
        """
        field.validate(value)
        if value is None:
            return None
        return PostgresqlVectorValues.format_vector_text(value)

    @staticmethod
    def to_python(field: Field[Any], value: Any) -> list[float] | None:
        """A vector read as pgvector's text or binary payload.

        Args:
            field: The vector field.
            value: The value read.

        Returns:
            The vector.
        """
        if isinstance(value, str):
            return PostgresqlVectorValues.parse_vector_text(value)
        return cast("list[float] | None", field.to_python(value))

    @staticmethod
    def format_vector_text(values: Sequence[float]) -> str:
        """Formats a list of floats as pgvector's bracketed text representation."""
        return "[" + ",".join(repr(float(component)) for component in values) + "]"

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
        return PostgresqlVectorValues.parse_binary_hex(stripped)

    @staticmethod
    def parse_binary_hex(raw: str) -> list[float]:
        """Parses pgvector's binary send/recv wire format: a big-endian uint16 element count, a
        reserved uint16 (always 0), then that many big-endian float4 values."""
        data = bytes.fromhex(raw)
        dimensions = struct.unpack(">H", data[0:2])[0]
        return list(struct.unpack(f">{dimensions}f", data[4 : 4 + dimensions * 4]))
