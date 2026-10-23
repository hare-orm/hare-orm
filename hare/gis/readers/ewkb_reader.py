from __future__ import annotations

import math
import struct

from hare.exceptions import ValidationError
from hare.gis.constants import (
    EWKB_M_FLAG,
    EWKB_SRID_FLAG,
    EWKB_TYPE_CODE_MASK,
    EWKB_Z_FLAG,
    GEOMETRY_MAX_NESTING_DEPTH,
    GEOMETRY_NESTING_DEPTH_MESSAGE,
    ISO_WKB_M_OFFSET,
    ISO_WKB_Z_OFFSET,
    ISO_WKB_ZM_OFFSET,
    WKB_BIG_ENDIAN,
    WKB_LITTLE_ENDIAN,
)
from hare.gis.enums import GeometryType
from hare.gis.geometries.geometry import Geometry
from hare.gis.geometries.geometry_collection import GeometryCollection
from hare.gis.geometries.line_string import LineString
from hare.gis.geometries.point import Point
from hare.gis.geometries.polygon import Polygon
from hare.gis.readers.constants import COLLECTION_CLASSES, GEOMETRY_TYPES_BY_CODE


class EwkbReader:
    """Reads a geometry from (E)WKB - the extended form with an SRID and a z flag, and ISO WKB
    with its ``1000``-offset type codes. Measured (m) coordinates are refused.

    Args:
        data: The bytes.
    """

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    @classmethod
    def read(cls, data: bytes | bytearray | memoryview | str) -> Geometry:
        """The geometry of (E)WKB bytes, or of their hex text.

        Args:
            data: The bytes or hex text.

        Returns:
            The geometry.

        Raises:
            ValidationError: ``data`` isn't a whole (E)WKB geometry.
        """
        try:
            raw = bytes.fromhex(data) if isinstance(data, str) else bytes(data)
            reader = cls(raw)
            geometry = reader.read_geometry(None)
        except (ValueError, struct.error, IndexError) as error:
            raise ValidationError(f"Not a WKB geometry: {error}") from None
        if reader.offset != len(raw):
            raise ValidationError(f"Not a WKB geometry: {len(raw) - reader.offset} byte(s) after its end")
        return geometry

    def read_struct(self, byte_order: str, format_text: str) -> tuple[float | int, ...]:
        """Reads values at the current offset and moves past them.

        Args:
            byte_order: ``<`` or ``>``.
            format_text: The ``struct`` format of the values.

        Returns:
            The values.
        """
        values_format = struct.Struct(byte_order + format_text)
        values = values_format.unpack_from(self.data, self.offset)
        self.offset += values_format.size
        return values

    def read_geometry(self, srid: int | None, depth: int = 0) -> Geometry:
        """Reads one geometry at the current offset - a member of a collection inherits its SRID.

        Args:
            srid: The enclosing geometry's SRID.
            depth: The collections it is nested in.

        Returns:
            The geometry.

        Raises:
            ValueError: The bytes aren't a geometry hare reads, or nest collections too deep.
        """
        byte_order_code = self.data[self.offset]
        self.offset += 1
        if byte_order_code not in {WKB_BIG_ENDIAN, WKB_LITTLE_ENDIAN}:
            raise ValueError(f"unknown byte order {byte_order_code}")
        byte_order = "<" if byte_order_code == WKB_LITTLE_ENDIAN else ">"
        type_code = int(self.read_struct(byte_order, "I")[0])
        has_z = bool(type_code & EWKB_Z_FLAG)
        has_m = bool(type_code & EWKB_M_FLAG)
        if type_code & EWKB_SRID_FLAG:
            (srid,) = self.read_struct(byte_order, "i")  # type: ignore[assignment]
        type_code &= EWKB_TYPE_CODE_MASK
        for offset, offset_has_z, offset_has_m in (
            (ISO_WKB_ZM_OFFSET, True, True),
            (ISO_WKB_M_OFFSET, False, True),
            (ISO_WKB_Z_OFFSET, True, False),
        ):
            if type_code > offset:
                type_code -= offset
                has_z, has_m = has_z or offset_has_z, has_m or offset_has_m
                break
        if has_m:
            raise ValueError("measured (M) coordinates are not supported")
        geometry_type = GEOMETRY_TYPES_BY_CODE.get(type_code)
        if geometry_type is None:
            raise ValueError(f"unknown geometry type code {type_code}")
        dimensions = 3 if has_z else 2
        geometry_srid = Geometry.get_validated_srid(srid)
        if geometry_type == GeometryType.POINT:
            position = self.read_struct(byte_order, "d" * dimensions)
            if all(math.isnan(number) for number in position):
                return Point(srid=geometry_srid)
            return Point(*position, srid=geometry_srid)
        if geometry_type == GeometryType.LINESTRING:
            return LineString(self.read_positions(byte_order, dimensions), srid=geometry_srid)
        if geometry_type == GeometryType.POLYGON:
            (ring_count,) = self.read_struct(byte_order, "I")
            rings = [self.read_positions(byte_order, dimensions) for _ring_index in range(int(ring_count))]
            return Polygon.from_coordinates(rings, srid=geometry_srid)
        if depth >= GEOMETRY_MAX_NESTING_DEPTH:
            raise ValueError(GEOMETRY_NESTING_DEPTH_MESSAGE.format(depth=GEOMETRY_MAX_NESTING_DEPTH))
        (member_count,) = self.read_struct(byte_order, "I")
        members = [self.read_geometry(geometry_srid, depth + 1) for _member_index in range(int(member_count))]
        collection_class = COLLECTION_CLASSES.get(geometry_type, GeometryCollection)
        return collection_class(members, srid=geometry_srid)

    def read_positions(self, byte_order: str, dimensions: int) -> list[tuple[float | int, ...]]:
        """Reads a count, then that many positions.

        Args:
            byte_order: ``<`` or ``>``.
            dimensions: The coordinates of a position.

        Returns:
            The positions.
        """
        (position_count,) = self.read_struct(byte_order, "I")
        return [self.read_struct(byte_order, "d" * dimensions) for _position_index in range(int(position_count))]
