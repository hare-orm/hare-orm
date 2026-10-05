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


class Holder(Model):
    """A holder of badges - awarded through a model whose key is taken from its series."""

    id = fields.BigIntField(primary_key=True, generated=False)
    badges: fields.ManyToManyRelation[Badge] = fields.ManyToManyField(
        "models.Badge", related_name="holders", through="models.BadgeAward"
    )


class BadgeAward(Model):
    """A badge awarded to a holder - its key taken from the model's series."""

    id = fields.BigIntField(primary_key=True)
    holder = fields.ForeignKeyField("models.Holder", related_name="awards")
    badge = fields.ForeignKeyField("models.Badge", related_name="awards")
