from __future__ import annotations

from hare import Model, fields
from hare.dialects.sqlite.indexes import SpatialiteIndex
from hare.gis import PointField, PolygonField


class IndexedShop(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    location = PointField()
    area = PolygonField(null=True)
    owner = fields.ForeignKeyField("models.IndexedOwner", related_name="shops", null=True)

    class Meta:
        table = "indexed_shop"
        indexes = [SpatialiteIndex(fields=("location",)), SpatialiteIndex(fields=("area",))]


class IndexedOwner(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "indexed_owner"


class IndexedRegion(Model):
    id = fields.IntField(primary_key=True)
    center = PointField(geography=True, srid=4269)

    class Meta:
        table = "indexed_region"


class IndexedCity(Model):
    id = fields.IntField(primary_key=True)
    center = PointField(geography=True)

    class Meta:
        table = "indexed_city"
        indexes = [SpatialiteIndex(fields=("center",))]
