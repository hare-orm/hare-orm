"""A model for the health check tests' own context - its tables are never made."""

from hare import fields
from hare.models import Model


class HealthProbe(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "health_probe"
