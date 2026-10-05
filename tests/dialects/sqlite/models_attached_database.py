from hare import Model, fields
from hare.ddl.enums import TriggerEvent
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.trigger import Trigger
from hare.dialects.sqlite.indexes import FullTextIndex


class ArchivedArticle(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=200)
    body = fields.TextField(null=True)
    edits = fields.IntField(default=0)

    class Meta:
        table = "archived_article"
        schema = "archive"
        indexes = (FullTextIndex(fields=("title", "body"), tokenizer="porter unicode61"),)
        triggers = (
            Trigger(
                name="archived_article_counts_edits",
                on=TriggerEvent.UPDATE,
                body=RawSQLTerm(
                    "UPDATE archived_article SET edits = edits + 1 WHERE id = new.id AND new.edits = old.edits;"
                ),
            ),
        )
