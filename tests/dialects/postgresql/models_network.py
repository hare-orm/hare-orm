from __future__ import annotations

from hare import Model, fields
from hare.dialects.postgresql.fields.network import CidrField, InetField, MacAddressField


class NetworkHost(Model):
    id = fields.IntField(primary_key=True)
    address = InetField()
    subnet = CidrField(null=True)
    mac = MacAddressField(null=True, unique=True)

    class Meta:
        table = "network_host"
