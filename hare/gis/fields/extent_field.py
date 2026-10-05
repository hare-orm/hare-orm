from __future__ import annotations

from typing import Any

from hare.exceptions import ValidationError
from hare.fields.field import Field
from hare.gis.constants import EXTENT_BOX_PATTERN
from hare.gis.geometries.geometry import Geometry


class ExtentField(Field[tuple[float, float, float, float]]):
    """The result of ``Extent`` - the bounding box of a group's geometries as ``(xmin, ymin, xmax,
    ymax)``, read from PostGIS's ``BOX(xmin ymin,xmax ymax)`` text or from the box's polygon."""

    field_type = tuple

    def get_python_type(self) -> Any:
        return tuple[float, float, float, float]

    @staticmethod
    def get_geometry_bounds(geometry: Geometry) -> tuple[float, float, float, float]:
        """The bounding box of a geometry's positions.

        Args:
            geometry: The geometry.

        Returns:
            ``(xmin, ymin, xmax, ymax)``.
        """
        positions = ExtentField.get_positions(geometry.get_coordinates())
        x_coordinates = [position[0] for position in positions]
        y_coordinates = [position[1] for position in positions]
        return min(x_coordinates), min(y_coordinates), max(x_coordinates), max(y_coordinates)

    @staticmethod
    def get_positions(coordinates: Any) -> list[tuple[float, ...]]:
        """Every position of nested coordinates - a collection's members included.

        Args:
            coordinates: A geometry's coordinates, or a member geometry.

        Returns:
            The positions.
        """
        if isinstance(coordinates, Geometry):
            return ExtentField.get_positions(coordinates.get_coordinates())
        if coordinates and isinstance(coordinates[0], float):
            return [coordinates]
        return [position for item in coordinates for position in ExtentField.get_positions(item)]

    def to_python(self, value: Any) -> tuple[float, float, float, float] | None:
        if value is None or isinstance(value, tuple):
            return value
        if isinstance(value, Geometry):
            return self.get_geometry_bounds(value) if not value.is_empty else None
        match = EXTENT_BOX_PATTERN.fullmatch(value) if isinstance(value, str) else None
        if match is None:
            raise ValidationError(f"{self.model_field_name}: {self.get_value_for_message(value)} is not a box")
        xmin, ymin, xmax, ymax = (float(number) for number in match.groups())
        return xmin, ymin, xmax, ymax
