"""Models of the tests of sensitive values never shown in logs, events and errors."""

from hare import fields
from hare.models import Model


class Account(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    passport = fields.CharField(max_length=50, sensitive=True)
    token = fields.TextField(null=True, sensitive=True)
