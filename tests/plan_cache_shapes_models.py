"""Models of the tests of the query shapes running on their plans."""

from hare import fields
from hare.fields import CASCADE, SET_NULL
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.models import Model


class Writer(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "shape_writer"


class Tag(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "shape_tag"


class Post(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    score = fields.IntField(default=0)
    writer = fields.ForeignKeyField("models.Writer", related_name="posts", null=True, on_delete=SET_NULL)
    tags = fields.ManyToManyField("models.Tag", related_name="posts")

    class Meta:
        table = "shape_post"


class Edition(Model):
    book_number = fields.IntField()
    number = fields.IntField()
    title = fields.CharField(max_length=50)
    pk = CompositePrimaryKey("book_number", "number")

    class Meta:
        table = "shape_edition"


class Review(Model):
    id = fields.IntField(primary_key=True)
    stars = fields.IntField()
    edition = fields.ForeignKeyField("models.Edition", related_name="reviews", on_delete=CASCADE)

    class Meta:
        table = "shape_review"


class Note(Model):
    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=50, default="")
    target = fields.GenericForeignKeyField(
        {"post": Post, "edition": Edition}, related_name="notes", on_delete=SET_NULL, null=True
    )

    class Meta:
        table = "shape_note"


class Category(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    parent = fields.ForeignKeyField("models.Category", related_name="children", null=True, on_delete=CASCADE)

    class Meta:
        table = "shape_category"


class Shelf(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    editions = fields.ManyToManyField("models.Edition", related_name="shelves")

    class Meta:
        table = "shape_shelf"
