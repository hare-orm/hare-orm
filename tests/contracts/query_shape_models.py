from hare import fields
from hare.models import Model
from hare.query.expressions import F
from tests.utils.database_under_test import DatabaseUnderTest


class ShapeShelf(Model):
    store = fields.CharField(max_length=10)
    number = fields.IntField()
    label = fields.CharField(max_length=10)
    pk = fields.CompositePrimaryKey("store", "number")


class ShapeWriter(Model):
    id = fields.IntField(primary_key=True, generated=False)
    name = fields.CharField(max_length=10)
    shelves = fields.ManyToManyField("models.ShapeShelf", related_name="writers", db_constraint=False)

    class Meta:
        ordering = ["items__id"]


class ShapeItem(Model):
    id = fields.IntField(primary_key=True, generated=False)
    score = fields.IntField(null=True)
    shelf = fields.ForeignKeyField("models.ShapeShelf", related_name="items", null=True, db_constraint=False)
    writer = fields.ForeignKeyField("models.ShapeWriter", related_name="items", null=True, db_constraint=False)


class ShapeReview(Model):
    id = fields.IntField(primary_key=True, generated=False)
    writer = fields.ForeignKeyField("models.ShapeWriter", related_name="reviews", db_constraint=False)


class ShapeBookmark(Model):
    id = fields.IntField(primary_key=True, generated=False)
    shelf = fields.OneToOneField("models.ShapeShelf", related_name="bookmark", null=True, db_constraint=False)


class ShapeScore(Model):
    id = fields.IntField(primary_key=True, generated=False)
    score = fields.IntField()
    doubled = fields.GeneratedField(F("score") * 2, output_field=fields.IntField())

    class Meta:
        returning = True


__models__ = [ShapeShelf, ShapeWriter, ShapeItem, ShapeReview, ShapeScore]
if DatabaseUnderTest.get_dialect().features.supports_unique_constraints:
    __models__.append(ShapeBookmark)
