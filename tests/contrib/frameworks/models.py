from hare import fields
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.models import Model


class Tag(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)


class Writer(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50, unique=True)
    tags = fields.ManyToManyField("models.Tag", related_name="writers")


class Chapter(Model):
    volume = fields.IntField()
    number = fields.IntField()
    writer = fields.ForeignKeyField("models.Writer", related_name="chapters")
    title = fields.CharField(max_length=100)

    pk = CompositePrimaryKey("volume", "number")


class Draft(Model):
    """A draft, maybe a revision of an earlier one."""

    id = fields.IntField(primary_key=True)
    earlier = fields.ForeignKeyField("models.Draft", related_name="revisions", null=True)


class Badge(Model):
    """A badge of a level - a field with a default."""

    id = fields.IntField(primary_key=True)
    level = fields.IntField(default=1)
