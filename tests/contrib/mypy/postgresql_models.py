"""The models of a PostgreSQL connection the mypy plugin tests type-check queries of - ranges,
geometries, addresses and paths, whose lookups take values of their own."""

from hare import fields
from hare.dialects.postgresql import fields as postgresql_fields
from hare.gis import PointField, PolygonField
from hare.models import Model


class Booking(Model):
    id = fields.IntField(primary_key=True)
    seats = postgresql_fields.IntRangeField()
    during = postgresql_fields.DateTimeRangeField()
    days = postgresql_fields.DateMultiRangeField()
    place = PointField(srid=4326)
    area = PolygonField(srid=4326)
    address = fields.IPAddressField()
    network = postgresql_fields.InetField()
    section = postgresql_fields.LtreeField()
    details = fields.JSONField()

    class Meta:
        app = "typing_postgresql"
