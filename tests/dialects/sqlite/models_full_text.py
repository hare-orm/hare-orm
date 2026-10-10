from hare import Model, fields
from hare.dialects.sqlite.indexes import FullTextIndex


class SearchArticle(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=200)
    body = fields.TextField(null=True)
    rating = fields.IntField(default=0)

    class Meta:
        table = "search_article"
        indexes = (FullTextIndex(fields=("title", "body"), tokenizer="porter unicode61"),)


class SearchComment(Model):
    id = fields.BigIntField(primary_key=True)
    article = fields.ForeignKeyField("models.SearchArticle", related_name="comments")
    text = fields.TextField()

    class Meta:
        table = "search_comment"
        indexes = (FullTextIndex(fields=("text",), name="search_comment_text"),)


class UnindexedNote(Model):
    id = fields.IntField(primary_key=True)
    text = fields.TextField()

    class Meta:
        table = "unindexed_note"
