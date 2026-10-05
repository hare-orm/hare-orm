"""
Testing Models for GeneratedField(stored=False) - VIRTUAL generated columns, SQLite-only
(Postgres only supports STORED).
"""

from hare import fields
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.fields.generated_field import GeneratedField
from hare.models import Model


class VirtualWidget(Model):
    length = fields.IntField()
    width = fields.IntField()
    area = GeneratedField(
        expression=RawSQLTerm("length * width"),
        output_field=fields.IntField(),
        stored=False,
    )
