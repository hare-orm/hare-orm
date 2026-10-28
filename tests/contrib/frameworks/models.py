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

    @property
    def initial(self) -> str:
        return self.name[:1].upper()

    def shouted_name(self) -> str:
        return self.name.upper()


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


class Memo(Model):
    """A memo stamped by the ORM on create and on each save."""

    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=50)
    level = fields.IntField(default=1)
    created = fields.DatetimeField(auto_now_add=True)
    updated = fields.DatetimeField(auto_now=True)
