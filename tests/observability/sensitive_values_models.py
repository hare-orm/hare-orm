"""Models of the tests of sensitive values never shown in logs, events and errors."""

from hare import fields
from hare.models import Model


class Account(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    passport = fields.CharField(max_length=50, sensitive=True)
    token = fields.TextField(null=True, sensitive=True)


class Badge(Model):
    id = fields.IntField(primary_key=True)
    code = fields.CharField(max_length=50, unique=True, sensitive=True)
    holder = fields.ForeignKeyField("models.Account", related_name="badges", null=True, sensitive=True)


class Profile(Model):
    id = fields.IntField(primary_key=True)
    settings = fields.JSONField(null=True, sensitive=True)


class RegionAccount(Model):
    region = fields.CharField(max_length=50, sensitive=True)
    number = fields.IntField()
    note = fields.CharField(max_length=50, default="")
    pk = fields.CompositePrimaryKey("region", "number")


class Passport(Model):
    code = fields.CharField(max_length=50, primary_key=True, sensitive=True)
    name = fields.CharField(max_length=50, default="")


class Visa(Model):
    id = fields.IntField(primary_key=True)
    passport = fields.ForeignKeyField("models.Passport", related_name="visas")


class Trip(Model):
    id = fields.IntField(primary_key=True)
    passports = fields.ManyToManyField("models.Passport", related_name="trips")


class Guide(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50, default="")


class Tour(Model):
    id = fields.IntField(primary_key=True)
    guide = fields.ForeignKeyField("models.Guide", related_name="tours")
