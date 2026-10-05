from __future__ import annotations

import struct
from typing import ClassVar

from hare.dialects.sqlite.spatial.constants import (
    SPATIALITE_BLOB_BIG_ENDIAN,
    SPATIALITE_BLOB_END,
    SPATIALITE_BLOB_ENTITY,
    SPATIALITE_BLOB_LITTLE_ENDIAN,
    SPATIALITE_BLOB_MBR_END,
    SPATIALITE_BLOB_MBR_END_OFFSET,
    SPATIALITE_BLOB_MIN_SIZE,
    SPATIALITE_BLOB_START,
    SPATIALITE_COMPRESSED_CLASS_OFFSET,
    SPATIALITE_M_CLASS_OFFSET,
    SPATIALITE_Z_CLASS_OFFSET,
)
from hare.exceptions import ValidationError
from hare.gis.constants import WKB_TYPE_CODES
from hare.gis.enums import GeometryType
from hare.gis.geometries.declarations import MultiLineString, MultiPoint, MultiPolygon
from hare.gis.geometries.geometry import Geometry
from hare.gis.geometries.geometry_collection import GeometryCollection
from hare.gis.geometries.line_string import LineString
from hare.gis.geometries.multi_geometry import MultiGeometry
from hare.gis.geometries.point import Point
from hare.gis.geometries.polygon import Polygon


class SpatialiteBlobReader:
    """Reads a geometry from SpatiaLite's own BLOB - the header with the byte order, the SRID and the
    bounding box, then the geometry class and its coordinates; a compressed line or ring keeps its
    inner positions as float offsets from the position before. Measured (m) coordinates are refused.

    Args:
        data: The bytes.
    """

    #: The geometry type of each class code.
    GEOMETRY_TYPES_BY_CODE = {code: geometry_type for geometry_type, code in WKB_TYPE_CODES.items()}
    #: The class of each multi geometry type - any other collection is a GeometryCollection.
    COLLECTION_CLASSES: ClassVar[dict[GeometryType, type[MultiGeometry]]] = {
        GeometryType.MULTIPOINT: MultiPoint,
        GeometryType.MULTILINESTRING: MultiLineString,
        GeometryType.MULTIPOLYGON: MultiPolygon,
    }

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0
        self.byte_order = "<"

    @classmethod
    def read(cls, data: bytes | bytearray | memoryview) -> Geometry:
        """The geometry of a SpatiaLite BLOB.

        Args:
            data: The bytes.

        Returns:
            The geometry.

        Raises:
            ValidationError: ``data`` isn't a whole SpatiaLite geometry.
        """
        try:
            return cls(bytes(data)).read_blob()
        except (ValueError, struct.error, IndexError) as error:
            raise ValidationError(f"Not a SpatiaLite geometry: {error}") from None

    def read_blob(self) -> Geometry:
        """Reads the whole BLOB.

        Raises:
            ValueError: The bytes aren't a SpatiaLite geometry hare reads.
        """
        data = self.data
        if (
            len(data) < SPATIALITE_BLOB_MIN_SIZE
            or data[0] != SPATIALITE_BLOB_START
            or data[SPATIALITE_BLOB_MBR_END_OFFSET] != SPATIALITE_BLOB_MBR_END
            or data[-1] != SPATIALITE_BLOB_END
        ):
            raise ValueError("no SpatiaLite header and end")
        if data[1] not in {SPATIALITE_BLOB_LITTLE_ENDIAN, SPATIALITE_BLOB_BIG_ENDIAN}:
            raise ValueError(f"unknown byte order {data[1]}")
        self.byte_order = "<" if data[1] == SPATIALITE_BLOB_LITTLE_ENDIAN else ">"
        self.offset = 2
        (srid,) = self.read_struct("i")
        self.offset = SPATIALITE_BLOB_MBR_END_OFFSET + 1
        (class_code,) = self.read_struct("I")
        geometry = self.read_body(int(class_code), Geometry.get_validated_srid(int(srid)))
        if self.offset != len(data) - 1:
            raise ValueError(f"{len(data) - 1 - self.offset} byte(s) after the geometry")
        return geometry

    def read_struct(self, format_text: str) -> tuple[float | int, ...]:
        """Reads values at the current offset and moves past them.

        Args:
            format_text: The ``struct`` format of the values.

        Returns:
            The values.
        """
        values_format = struct.Struct(self.byte_order + format_text)
        values = values_format.unpack_from(self.data, self.offset)
        self.offset += values_format.size
        return values

    def read_body(self, class_code: int, srid: int | None) -> Geometry:
        """Reads the geometry of a class code at the current offset.

        Args:
            class_code: The class code.
            srid: The SRID of the BLOB.

        Returns:
            The geometry.

        Raises:
            ValueError: The class is unknown or has measured coordinates.
        """
        compressed = class_code > SPATIALITE_COMPRESSED_CLASS_OFFSET
        if compressed:
            class_code -= SPATIALITE_COMPRESSED_CLASS_OFFSET
        # An m class (2001...) and a z and m one (3001...) are both past the m offset.
        if class_code > SPATIALITE_M_CLASS_OFFSET:
            raise ValueError("measured (M) coordinates are not supported")
        has_z = class_code > SPATIALITE_Z_CLASS_OFFSET
        if has_z:
            class_code -= SPATIALITE_Z_CLASS_OFFSET
        geometry_type = self.GEOMETRY_TYPES_BY_CODE.get(class_code)
        if geometry_type is None:
            raise ValueError(f"unknown geometry class {class_code}")
        dimensions = 3 if has_z else 2
        if geometry_type == GeometryType.POINT:
            return Point(*self.read_struct("d" * dimensions), srid=srid)
        if geometry_type == GeometryType.LINESTRING:
            return LineString(self.read_positions(dimensions, compressed), srid=srid)
        if geometry_type == GeometryType.POLYGON:
            (ring_count,) = self.read_struct("I")
            rings = [self.read_positions(dimensions, compressed) for _ring_index in range(int(ring_count))]
            return Polygon.from_coordinates(rings, srid=srid)
        (member_count,) = self.read_struct("I")
        members = []
        for _member_index in range(int(member_count)):
            if self.data[self.offset] != SPATIALITE_BLOB_ENTITY:
                raise ValueError("a collection member without its marker")
            self.offset += 1
            (member_class_code,) = self.read_struct("I")
            members.append(self.read_body(int(member_class_code), srid))
        collection_class = self.COLLECTION_CLASSES.get(geometry_type, GeometryCollection)
        return collection_class(members, srid=srid)

    def read_positions(self, dimensions: int, compressed: bool) -> list[tuple[float, ...]]:
        """Reads a count, then that many positions - of a compressed geometry the first and the last
        in full, every other as float offsets from the one before.

        Args:
            dimensions: The coordinates of a position.
            compressed: Whether the geometry is compressed.

        Returns:
            The positions.
        """
        (position_count,) = self.read_struct("I")
        last_index = int(position_count) - 1
        positions: list[tuple[float, ...]] = []
        for position_index in range(int(position_count)):
            if not compressed or position_index in (0, last_index):
                positions.append(tuple(float(number) for number in self.read_struct("d" * dimensions)))
                continue
            offsets = self.read_struct("f" * dimensions)
            previous = positions[-1]
            positions.append(tuple(previous[axis] + float(offsets[axis]) for axis in range(dimensions)))
        return positions
