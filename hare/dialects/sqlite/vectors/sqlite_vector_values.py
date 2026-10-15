from __future__ import annotations

import json
from array import array
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.sqlite.vectors.constants import SQLITE_VECTOR_ARRAY_TYPECODE, SQLITE_VECTOR_MAX_DIMENSIONS
from hare.exceptions import UnSupportedError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.models import Model
    from hare.vectors.vector_field import VectorField


class SqliteVectorValues:
    """How SQLite stores a ``VectorField`` - sqlite-vec's float32 vector in a ``BLOB``, written and
    read without the extension."""

    @staticmethod
    def raise_if_too_long(field: Field[Any]) -> None:
        """Rejects a vector longer than sqlite-vec takes.

        Args:
            field: The vector field.

        Raises:
            UnSupportedError: The field has more dimensions than sqlite-vec takes.
        """
        dimensions = cast("VectorField", field).dimensions
        if dimensions > SQLITE_VECTOR_MAX_DIMENSIONS:
            raise UnSupportedError(
                f"{field.model_field_name}: a vector of {dimensions} dimensions doesn't fit SQLite - sqlite-vec "
                f"takes at most {SQLITE_VECTOR_MAX_DIMENSIONS}"
            )

    @staticmethod
    def get_column_type(field: Field[Any]) -> str:
        """``BLOB``.

        Args:
            field: The vector field.

        Returns:
            The column type.

        Raises:
            UnSupportedError: The field has more dimensions than sqlite-vec takes.
        """
        SqliteVectorValues.raise_if_too_long(field)
        return "BLOB"

    @staticmethod
    def to_db(field: Field[Any], value: Sequence[float] | None, instance: type[Model] | Model) -> bytes | None:
        """The vector as sqlite-vec's float32 vector.

        Args:
            field: The vector field.
            value: The vector.
            instance: The instance or model written.

        Returns:
            The bytes, None for no vector.

        Raises:
            ValidationError: The value doesn't fit the field.
            UnSupportedError: The field has more dimensions than sqlite-vec takes.
        """
        SqliteVectorValues.raise_if_too_long(field)
        field.validate(value)
        if value is None:
            return None
        return SqliteVectorValues.get_vector_bytes(value)

    @staticmethod
    def get_vector_bytes(values: Sequence[float]) -> bytes:
        """A list of floats as sqlite-vec's float32 vector.

        Args:
            values: The elements.

        Returns:
            The bytes.
        """
        return array(SQLITE_VECTOR_ARRAY_TYPECODE, [float(element) for element in values]).tobytes()

    @staticmethod
    def to_python(field: Field[Any], value: Any) -> list[float] | None:
        """A vector read as sqlite-vec's float32 vector, or as the JSON text sqlite-vec takes too.

        Args:
            field: The vector field.
            value: The value read.

        Returns:
            The vector.

        Raises:
            ValidationError: JSON text that isn't a list of numbers.
        """
        if isinstance(value, (bytes, bytearray, memoryview)):
            return cast("list[float]", array(SQLITE_VECTOR_ARRAY_TYPECODE, bytes(value)).tolist())
        if isinstance(value, str):
            validation_error = None
            try:
                elements = [float(element) for element in json.loads(value)]
            except (TypeError, ValueError) as error:
                validation_error = field.get_validation_error(error, value)
            if validation_error is not None:
                raise validation_error
            return elements
        return cast("list[float] | None", field.to_python(value))
