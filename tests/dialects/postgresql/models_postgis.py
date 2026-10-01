from typing import Any

from hare import Model, fields
from hare.dialects.postgresql.fields.gis import PostGISField
from hare.dialects.postgresql.indexes import GistIndex
from hare.exceptions import ValidationError


class Place(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    location = PostGISField()

    class Meta:
        table = "postgis_place"
        indexes = (GistIndex(fields=("location",)),)


def reject_southern_hemisphere(value: Any) -> None:
    latitude, _longitude = value
    if latitude < 0:
        raise ValidationError("latitude must be in the northern hemisphere")


class PlaceWithValidatedLocation(Model):
    """Same shape as Place, but with a custom validators=[...] on the field - used to prove
    __within_km/STDistance/STDWithin actually run it (they used to build the geography literal
    straight from the raw query point, skipping to_db_value()/validate() entirely for the QUERY
    point, even though create()/save() already run it for a STORED one)."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    location = PostGISField(validators=[reject_southern_hemisphere])

    class Meta:
        table = "postgis_place_validated"
        indexes = (GistIndex(fields=("location",)),)
