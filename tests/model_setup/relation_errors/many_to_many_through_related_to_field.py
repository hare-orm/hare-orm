"""A ManyToManyField(through=Model) whose through model's FK to the related side uses a to_field=
other than that model's primary key."""

from hare import fields
from hare.models import Model


class Club(Model):
    name = fields.CharField(max_length=20)
    players: fields.ManyToManyRelation["Player"] = fields.ManyToManyField("models.Player", through="models.Seat")


class Player(Model):
    handle = fields.CharField(max_length=20, unique=True)


class Seat(Model):
    club = fields.ForeignKeyField("models.Club", related_name="seats")
    player = fields.ForeignKeyField("models.Player", related_name="seats", to_field="handle")
