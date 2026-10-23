from __future__ import annotations

import struct
from typing import Any, ClassVar

from hare.core.caching.cache import Cache
from hare.exceptions import ValidationError
from hare.gis.constants import (
    EWKB_SRID_FLAG,
    EWKB_SRID_SIZE,
    EWKB_TYPE_CODE_MASK,
    HEX_WKB_PATTERN,
    ISO_WKB_Z_OFFSET,
    WKB_HEADER_SIZE,
    WKB_LITTLE_ENDIAN,
    WKB_TYPE_CODES,
)
from hare.gis.enums import GeometryType
from hare.gis.fields.geometry_field import GeometryField
from hare.gis.geometries.geometry import Geometry


class MadeLineField(GeometryField):
    """The line ``MakeLine`` makes of a group's points - a group of one point is read as the database
    gives it: NULL as None, a line of one position, which no ``LineString`` is, refused with that
    reason."""

    #: (srid, dimensions) -> the shared field of lines of such points - a plan holds an output field
    #: weakly, so a fresh one per call would be collected at once.
    fields: ClassVar[Cache[MadeLineField]] = Cache(Cache.max_size_from_env(), holds_sql=False, keyed_by_model=False)

    @classmethod
    def get_for(cls, point_field: GeometryField) -> MadeLineField:
        """The field of the lines made of a field's points.

        Args:
            point_field: The points' field.

        Returns:
            The shared field, of the points' SRID and dimensions.
        """
        key = (point_field.srid, point_field.dimensions)
        field = cls.fields.get(key)
        if field is None:
            field = cls(GeometryType.LINESTRING, srid=point_field.srid, dimensions=point_field.dimensions)
            cls.fields[key] = field
        return field

    def to_python(self, value: Any) -> Geometry | None:
        try:
            return super().to_python(value)
        except ValidationError as error:
            # Only a value that isn't a geometry is looked into again.
            if self.is_single_position_line(value):
                raise ValidationError(
                    "MakeLine made a line of one position of a group of one point, which no LineString is"
                ) from error
            raise

    @staticmethod
    def is_single_position_line(value: Any) -> bool:
        """Whether a value is the (E)WKB of a line of one position.

        Args:
            value: The value read - bytes or their hex text.

        Returns:
            True for such a line.
        """
        if isinstance(value, str):
            if not HEX_WKB_PATTERN.fullmatch(value):
                return False
            data = bytes.fromhex(value)
        elif isinstance(value, (bytes, bytearray, memoryview)):
            data = bytes(value)
        else:
            return False
        byte_order = "<" if data[:1] == bytes((WKB_LITTLE_ENDIAN,)) else ">"
        try:
            (type_code,) = struct.unpack_from(byte_order + "I", data, 1)
            count_offset = WKB_HEADER_SIZE + (EWKB_SRID_SIZE if type_code & EWKB_SRID_FLAG else 0)
            (position_count,) = struct.unpack_from(byte_order + "I", data, count_offset)
        except struct.error:
            return False
        is_line = (type_code & EWKB_TYPE_CODE_MASK) % ISO_WKB_Z_OFFSET == WKB_TYPE_CODES[GeometryType.LINESTRING]
        return is_line and position_count == 1
