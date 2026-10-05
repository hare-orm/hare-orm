from hare import fields
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.models import Model


class RelationKeyBook(Model):
    id = fields.IntField(primary_key=True, generated=False)
    title = fields.CharField(max_length=50)


class RelationKeyEdition(Model):
    book = fields.ForeignKeyField("models.RelationKeyBook", related_name="editions")
    number = fields.IntField()
    pages = fields.IntField(null=True)
    pk = CompositePrimaryKey("book_id", "number")


class RelationKeyShelfSlot(Model):
    book = fields.ForeignKeyField("models.RelationKeyBook", related_name="slots")
    slot = fields.IntField()
    pk = CompositePrimaryKey("slot", "book")


class RelationKeyNote(Model):
    edition = fields.ForeignKeyField("models.RelationKeyEdition", related_name="notes")
    position = fields.IntField()
    text = fields.CharField(max_length=50)
    pk = CompositePrimaryKey("edition", "position")


class RelationKeyChapter(Model):
    """A relation to a key holding a relation's column, from a model set up before the target."""

    id = fields.IntField(primary_key=True, generated=False)
    edition = fields.ForeignKeyField("models.RelationKeyEdition", related_name="chapters")
