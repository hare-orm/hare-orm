"""
This example demonstrates SQL Schema generation for each DB type supported.
"""

from uuid import uuid4

from hare import fields
from hare.ddl.constraints import UniqueConstraint
from hare.fields import RESTRICT
from hare.models import Model


class Tournament(Model):
    tid = fields.SmallIntField(primary_key=True)
    name = fields.CharField(max_length=100, description="Tournament name", db_index=True)
    created = fields.DatetimeField(auto_now_add=True, description="Created */'`/* datetime")

    class Meta:
        table_description = "What Tournaments */'`/* we have"


class Event(Model):
    id = fields.BigIntField(primary_key=True, description="Event ID")
    name = fields.TextField()
    tournament: fields.ForeignKeyRelation[Tournament] = fields.ForeignKeyField(
        "models.Tournament", related_name="events", description="FK to tournament"
    )
    participants: fields.ManyToManyRelation[Team] = fields.ManyToManyField(
        "models.Team",
        related_name="events",
        through="teamevents",
        description="How participants relate",
        # Not SET_NULL/SET_DEFAULT - the through-table's own columns are always NOT NULL with
        # no default (M2M_TABLE_TEMPLATE), so those two are rejected at field-definition time.
        # RESTRICT still exercises a non-default on_delete value on a ManyToManyField.
        on_delete=RESTRICT,
    )
    modified = fields.DatetimeField(auto_now=True)
    prize = fields.DecimalField(max_digits=10, decimal_places=2, null=True)
    token = fields.CharField(max_length=100, description="Unique token", unique=True)
    key = fields.CharField(max_length=100)

    class Meta:
        table_description = "This table contains a list of all the events"
        # "name" is a TextField (indexable = False) - can't participate in a unique_together
        # constraint, mirroring the same rule a single TextField(unique=True) is already
        # subject to.
        constraints = (UniqueConstraint(fields=("tournament", "key")),)


class Team(Model):
    name = fields.CharField(max_length=50, primary_key=True, description="The TEAM name (and PK)")
    key = fields.IntField()
    manager: fields.ForeignKeyNullableRelation[Team] = fields.ForeignKeyField(
        "models.Team", related_name="team_members", null=True
    )
    talks_to: fields.ManyToManyRelation[Team] = fields.ManyToManyField("models.Team", related_name="gets_talked_to")

    class Meta:
        table_description = "The TEAMS!"
        indexes = [("manager", "key"), ["manager_id", "name"]]


class Address(Model):
    """
    The Team's address

    This is a long section of the docs that won't appear in the description.
    """

    city = fields.CharField(max_length=50, description="City")
    country = fields.CharField(max_length=50, description="Country")
    street = fields.CharField(max_length=128, description="Street Address")
    team: fields.OneToOneRelation[Team] = fields.OneToOneField(
        "models.Team", related_name="address", on_delete=fields.CASCADE, primary_key=True
    )


class Location(Model):
    name = fields.CharField(max_length=128)
    # This is just a comment
    #: No. of seats
    #: All this should not be part of the field description either!
    capacity = fields.IntField()
    rent = fields.FloatField()
    team: fields.OneToOneNullableRelation[Team] = fields.OneToOneField(
        "models.Team", on_delete=fields.SET_NULL, null=True
    )


class SourceFields(Model):
    id = fields.IntField(primary_key=True, source_field="sometable_id")
    chars = fields.CharField(max_length=255, source_field="some_chars_table", db_index=True)

    fk: fields.ForeignKeyNullableRelation[SourceFields] = fields.ForeignKeyField(
        "models.SourceFields", related_name="team_members", null=True, source_field="fk_sometable"
    )

    rel_to: fields.ManyToManyRelation[SourceFields] = fields.ManyToManyField(
        "models.SourceFields",
        related_name="rel_from",
        through="sometable_self",
        forward_key="sts_forward",
        backward_key="backward_sts",
    )

    class Meta:
        table = "sometable"
        indexes = [["chars"]]


class Group(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()
    uuid = fields.UUIDField(unique=True, default=uuid4)

    employees: fields.ReverseRelation[Employee]


class Employee(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()
    group: fields.ForeignKeyRelation[Group] = fields.ForeignKeyField(
        "models.Group",
        related_name="employees",
        to_field="uuid",
    )


class DefaultPK(Model):
    val = fields.IntField()


class ZeroMixin:
    zero = fields.IntField()


class OneMixin(ZeroMixin):
    one = fields.CharField(40, null=True)


class TwoMixin:
    two = fields.CharField(40)


class AbstractModel(Model, OneMixin):
    new_field = fields.CharField(max_length=100)

    class Meta:
        abstract = True


class InheritedModel(AbstractModel, TwoMixin):
    name = fields.TextField()
