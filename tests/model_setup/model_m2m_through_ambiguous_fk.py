"""A ManyToManyField(through=Model) whose through model has two ForeignKeyFields pointing at
the same side of the relation - hare can't tell which one is the intended link."""

from hare import fields
from hare.models import Model


class Person(Model):
    name = fields.CharField(max_length=20)
    groups: fields.ManyToManyRelation["Group"] = fields.ManyToManyField("models.Group", through="models.Membership")


class Group(Model):
    name = fields.CharField(max_length=20)


class Membership(Model):
    person = fields.ForeignKeyField(Person, related_name="membership_rows")
    backup_person = fields.ForeignKeyField(Person, related_name="backup_membership_rows")
    group = fields.ForeignKeyField(Group, related_name="membership_rows")
