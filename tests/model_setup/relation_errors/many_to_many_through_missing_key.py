"""A ManyToManyField(through=Model) whose through model has no ForeignKeyField at all pointing
at one side of the relation."""

from hare import fields
from hare.models import Model


class Person(Model):
    name = fields.CharField(max_length=20)
    groups: fields.ManyToManyRelation["Group"] = fields.ManyToManyField("models.Group", through="models.Membership")


class Group(Model):
    name = fields.CharField(max_length=20)


class Membership(Model):
    person = fields.ForeignKeyField(Person, related_name="membership_rows")
    note = fields.CharField(max_length=20, null=True)
