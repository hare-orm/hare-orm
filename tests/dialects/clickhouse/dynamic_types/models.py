"""Models of the ClickHouse Variant and Dynamic tests - columns holding values of several types, which
a server of ClickHouse 25.3 has."""

from hare import fields
from hare.dialects.clickhouse.fields import DynamicField, VariantField
from hare.models import Model


class Sample(Model):
    """Values of several types - ``Dynamic`` and ``Variant``."""

    id = fields.BigIntField(primary_key=True, generated=False)
    anything = DynamicField(null=True)
    reading = VariantField(
        [fields.IntField(), fields.CharField(max_length=20), fields.ArrayField(fields.FloatField())], null=True
    )
    limited = DynamicField(max_types=2, null=True)
    bag = fields.ArrayField(DynamicField(null=True))
