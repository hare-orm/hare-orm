from __future__ import annotations

import struct
from typing import Any

from hare.dialects.sqlite.spatial.constants import (
    SPATIALITE_BLOB_END,
    SPATIALITE_BLOB_ENTITY,
    SPATIALITE_BLOB_LITTLE_ENDIAN,
    SPATIALITE_BLOB_MBR_END,
    SPATIALITE_BLOB_START,
    SPATIALITE_Z_CLASS_OFFSET,
)
from hare.exceptions import ValidationError
from hare.gis.constants import WKB_TYPE_CODES
from hare.gis.enums import GeometryType
from hare.gis.fields.extent_field import ExtentField
from hare.gis.geometries.geometry import Geometry


class SpatialiteBlobWriter:
    """Writes a geometry as SpatiaLite's own BLOB, little-endian and uncompressed - the bytes
    SpatiaLite's own ``GeomFromEWKT()`` makes of the geometry. SpatiaLite has no empty geometry."""

    #: The header - the first byte, the byte order, the SRID, the bounding box and its end.
    HEADER = struct.Struct("<BBi4dB")

    @classmethod
    def write(cls, geometry: Geometry) -> bytes:
        """The BLOB of a geometry.

        Args:
            geometry: The geometry, with an SRID.

        Returns:
            The bytes.

        Raises:
            ValidationError: The geometry is empty or has an empty member.
        """
        cls.raise_if_empty(geometry)
        positions = ExtentField.get_positions(geometry.get_coordinates())
        header = cls.HEADER.pack(
            SPATIALITE_BLOB_START,
            SPATIALITE_BLOB_LITTLE_ENDIAN,
            geometry.srid or 0,
            min(position[0] for position in positions),
            min(position[1] for position in positions),
            max(position[0] for position in positions),
            max(position[1] for position in positions),
            SPATIALITE_BLOB_MBR_END,
        )
        return header + cls.get_body(geometry) + bytes((SPATIALITE_BLOB_END,))

    @classmethod
    def raise_if_empty(cls, geometry: Geometry) -> None:
        """Rejects an empty geometry, or a collection with an empty member.

        Args:
            geometry: The geometry.

        Raises:
            ValidationError: It is empty or has an empty member.
        """
        if geometry.is_empty:
            raise ValidationError(f"SpatiaLite stores no empty geometry, got {geometry.ewkt}")
        for member in getattr(geometry, "members", ()):
            cls.raise_if_empty(member)

    @classmethod
    def get_body(cls, geometry: Geometry) -> bytes:
        """A geometry's class code and coordinates.

        Args:
            geometry: The geometry.

        Returns:
            The bytes.
        """
        class_code = WKB_TYPE_CODES[geometry.GEOMETRY_TYPE] + (SPATIALITE_Z_CLASS_OFFSET if geometry.has_z else 0)
        body = struct.pack("<I", class_code)
        geometry_type = geometry.GEOMETRY_TYPE
        if geometry_type == GeometryType.POINT:
            return body + cls.get_position_bytes(geometry.get_coordinates())
        if geometry_type == GeometryType.LINESTRING:
            return body + cls.get_positions_bytes(geometry.get_coordinates())
        if geometry_type == GeometryType.POLYGON:
            rings = geometry.get_coordinates()
            return body + struct.pack("<I", len(rings)) + b"".join(cls.get_positions_bytes(ring) for ring in rings)
        members = geometry.members  # type: ignore[attr-defined]
        return (
            body
            + struct.pack("<I", len(members))
            + b"".join(bytes((SPATIALITE_BLOB_ENTITY,)) + cls.get_body(member) for member in members)
        )

    @staticmethod
    def get_position_bytes(position: Any) -> bytes:
        """The coordinates of one position as doubles."""
        return struct.pack(f"<{len(position)}d", *position)

    @classmethod
    def get_positions_bytes(cls, positions: Any) -> bytes:
        """A count, then the positions."""
        return struct.pack("<I", len(positions)) + b"".join(cls.get_position_bytes(position) for position in positions)
