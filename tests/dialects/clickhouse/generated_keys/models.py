"""Models of the ClickHouse generated keys tests - keys taken from the model's series of numbers, which a
server of ClickHouse 25.1 with a ClickHouse Keeper keeps."""

from hare import fields
from hare.models import Model


class Ticket(Model):
    """A ticket - its key taken from the model's series of numbers."""

    id = fields.IntField(primary_key=True)
    subject = fields.CharField(max_length=50)


class Badge(Model):
    """A badge - its key a small number taken from the model's series."""

    id = fields.SmallIntField(primary_key=True)
    label = fields.CharField(max_length=20)
