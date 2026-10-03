"""Models the columnar dialect's own tests run on."""

import uuid

from hare import fields
from hare.ddl.constraints import UniqueConstraint
from hare.ddl.indexes import Index
from hare.dialects.sqlite.table_options import SqliteTableOptions
from hare.models import Model
from tests.dialects.columnar.table_options import ColumnarTableOptions


class Shelf(Model):
    name = fields.CharField(max_length=50, unique=True)
    room = fields.CharField(max_length=50, default="")

    class Meta:
        constraints = [
            UniqueConstraint(fields=("name", "room")),
            UniqueConstraint(fields=("room", "name"), name="shelf_room_name"),
        ]
        indexes = [Index(fields=("room",), unique=True)]
        # SQLite's own entry - refused there for a generated primary key, ignored on columnar.
        table_options = [SqliteTableOptions(without_rowid=True)]


class Volume(Model):
    title = fields.CharField(max_length=100)
    code = fields.UUIDField(default=uuid.uuid4)
    shelf = fields.ForeignKeyField("models.Shelf", related_name="volumes", on_delete=fields.CASCADE)


class Keeper(Model):
    name = fields.CharField(max_length=50)
    shelf = fields.ForeignKeyField("models.Shelf", related_name="keepers", on_delete=fields.PROTECT)


class Reading(Model):
    title = fields.TextField()
    pages = fields.IntField()

    class Meta:
        primary_key = None
        table_options = [ColumnarTableOptions(strict=True)]


class Pinned(Model):
    id = fields.IntField(primary_key=True, generated=False)
    note = fields.TextField()

    class Meta:
        table_options = [ColumnarTableOptions(without_rowid=True)]
