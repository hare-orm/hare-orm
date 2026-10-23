"""Models of the ClickHouse geometry tests - points, lines and areas."""

from hare import fields, gis
from hare.models import Model


class Place(Model):
    """Geometries - x/y points, lines and areas."""

    id = fields.BigIntField(primary_key=True, generated=False)
    location = gis.PointField(geography=True)
    spot = gis.PointField(srid=3857)
    area = gis.PolygonField(srid=3857)
    zones = gis.MultiPolygonField(geography=True)
    route = gis.LineStringField(srid=3857)
    routes = gis.MultiLineStringField(srid=3857)


class Visit(Model):
    """A visit to a place - a point and an array of a row a place may have none of."""

    id = fields.BigIntField(primary_key=True, generated=False)
    place: fields.ForeignKeyRelation[Place] = fields.ForeignKeyField(
        "models.Place", related_name="visits", db_constraint=False
    )
    stop = gis.PointField(srid=3857)
    marks = fields.ArrayField(fields.IntField())
