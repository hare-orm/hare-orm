"""The models of the tests running on several databases at once - the few of tests.shared_models.testmodels they read,
so a database each test creates holds a handful of tables instead of the whole test schema."""

import binascii
import os

from hare import fields
from hare.models import Model


def generate_token():
    return binascii.hexlify(os.urandom(16)).decode("ascii")


class Author(Model):
    name = fields.CharField(max_length=255)


class Book(Model):
    name = fields.CharField(max_length=255)
    author: fields.ForeignKeyRelation[Author] = fields.ForeignKeyField(Author, related_name="books")
    rating = fields.FloatField()
    subject = fields.CharField(max_length=255, null=True)


class Tournament(Model):
    id = fields.SmallIntField(primary_key=True)
    name = fields.CharField(max_length=255)
    desc = fields.TextField(null=True)
    created = fields.DatetimeField(auto_now_add=True, db_index=True)

    events: fields.ReverseRelation["Event"]

    def __str__(self):
        return self.name


class Reporter(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()

    events: fields.ReverseRelation["Event"]

    class Meta:
        table = "re_port_er"

    def __str__(self):
        return self.name


class Event(Model):
    event_id = fields.BigIntField(primary_key=True)
    name = fields.TextField()
    tournament: fields.ForeignKeyRelation[Tournament] = fields.ForeignKeyField(
        to="models.Tournament", related_name="events"
    )
    reporter: fields.ForeignKeyNullableRelation[Reporter] = fields.ForeignKeyField(to=Reporter, null=True)
    participants: fields.ManyToManyRelation["Team"] = fields.ManyToManyField(
        "models.Team",
        related_name="events",
        through="event_team",
        backward_key="idEvent",
    )
    modified = fields.DatetimeField(auto_now=True)
    token = fields.TextField(default=generate_token)
    alias = fields.IntField(null=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Team(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()

    events: fields.ManyToManyRelation[Event]
    alias = fields.IntField(null=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.name


class CharPkModel(Model):
    id = fields.CharField(max_length=64, primary_key=True)


class EventTwo(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()
    tournament_id = fields.IntField()
    # A relation to events.TeamTwo, not to models.Team.
    participants: fields.ManyToManyRelation["TeamTwo"] = fields.ManyToManyField("events.TeamTwo")

    class Meta:
        app = "events"

    def __str__(self):
        return self.name


class TeamTwo(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()

    eventtwo_through: fields.ManyToManyRelation[EventTwo]

    class Meta:
        app = "events"

    def __str__(self):
        return self.name
