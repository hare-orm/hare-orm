from __future__ import annotations

from hare import Model, fields
from hare.dialects.postgresql.indexes import GistIndex
from hare.gis import GeometryField, LineStringField, PointField, PolygonField


class GisPlace(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    location = PointField()
    area = PolygonField(null=True)
    route = LineStringField(null=True)
    shape = GeometryField(null=True)
    height_point = PointField(dimensions=3, null=True)

    class Meta:
        table = "gis_place"
        indexes = (GistIndex(fields=("location",)), GistIndex(fields=("area",)))


class GisCity(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    center = PointField(geography=True)
    boundary = PolygonField(geography=True, null=True)

    class Meta:
        table = "gis_city"
        indexes = (GistIndex(fields=("center",)),)


class GisProjectedSite(Model):
    id = fields.IntField(primary_key=True)
    position = PointField(srid=3857)

    class Meta:
        table = "gis_projected_site"
