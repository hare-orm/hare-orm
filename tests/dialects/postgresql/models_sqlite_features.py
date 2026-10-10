from hare import Model, fields
from hare.dialects.sqlite.sqlite_table_options import SqliteTableOptions


class SqliteFeatureDocument(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=100)
    body = fields.TextField(null=True)

    class Meta:
        table = "sqlite_feature_document"
        # SQLite's options - a PostgreSQL connection creates the table without them.
        table_options = [SqliteTableOptions(strict=True)]
