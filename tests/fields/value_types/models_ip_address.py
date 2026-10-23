"""A model of the IP address and time of day ordering tests."""

from hare import fields
from hare.models import Model


class Host(Model):
    id = fields.IntField(primary_key=True, generated=False)
    address = fields.IPAddressField(null=True)
    opens = fields.TimeField(null=True)

    class Meta:
        table = "ordered_host"
