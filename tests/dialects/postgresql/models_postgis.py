from typing import Any

from hare import Model, fields
from hare.dialects.postgresql.indexes import GistIndex
from hare.exceptions import ValidationError
from hare.gis import PointField


class Place(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    location = PointField(geography=True, srid=4326)

    class Meta:
        table = "postgis_place"
        indexes = (GistIndex(fields=("location",)),)


def reject_southern_hemisphere(value: Any) -> None:
    if value.y < 0:
        raise ValidationError("latitude must be in the northern hemisphere")


class PlaceWithValidatedLocation(Model):
    """A geography whose validator applies to stored and compared Python points."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    location = PointField(geography=True, srid=4326, validators=[reject_southern_hemisphere])

    class Meta:
        table = "postgis_place_validated"
        indexes = (GistIndex(fields=("location",)),)
