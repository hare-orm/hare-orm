from __future__ import annotations

from hare import Model, fields
from hare.gis import GeometryField, LineStringField, PointField, PolygonField


class SpatialPlace(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    location = PointField()
    area = PolygonField(null=True)
    route = LineStringField(null=True)
    shape = GeometryField(null=True)
    height_point = PointField(dimensions=3, null=True)

    class Meta:
        table = "spatial_place"


class SpatialCity(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    center = PointField(geography=True)
    boundary = PolygonField(geography=True, null=True)

    class Meta:
        table = "spatial_city"


class SpatialSite(Model):
    id = fields.IntField(primary_key=True)
    position = PointField(srid=3857)

    class Meta:
        table = "spatial_site"
