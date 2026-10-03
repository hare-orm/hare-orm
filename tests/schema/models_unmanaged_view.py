"""A managed table and a Meta.managed = False model reading it through a view."""

from hare import fields
from hare.models import Model


class ViewSourceRow(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "view_source_row"


class ViewRow(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "view_row"
        managed = False
