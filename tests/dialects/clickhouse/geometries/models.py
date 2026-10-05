"""Models of the ClickHouse geometry tests - points, lines and areas; the line types come after
ClickHouse 24.3."""

from hare import fields, gis
from hare.models import Model


class Place(Model):
    """Geometries - x/y points, lines and areas."""

    id = fields.BigIntField(primary_key=True, generated=False)
    location = gis.PointField(geography=True)
    spot = gis.PointField(srid=3857)
    area = gis.PolygonField(srid=3857, null=True)
    zones = gis.MultiPolygonField(geography=True, null=True)
    route = gis.LineStringField(srid=3857, null=True)
    routes = gis.MultiLineStringField(srid=3857, null=True)
