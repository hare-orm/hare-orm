"""
Testing Models for GeneratedField(stored=False) - VIRTUAL generated columns, SQLite-only
(Postgres only supports STORED).
"""

from hare import fields
from hare.fields.generated import GeneratedField
from hare.models import Model


class VirtualWidget(Model):
    length = fields.IntField()
    width = fields.IntField()
    area = GeneratedField(
        expression="length * width",
        output_field=fields.IntField(),
        stored=False,
    )
