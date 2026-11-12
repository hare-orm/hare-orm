"""Single and composite keys sharing the schema of two independent connections."""

from hare import fields
from hare.models import Model


class Author(Model):
    name = fields.TextField()
    selected_books = fields.ManyToManyField("models.Book", related_name="readers", db_constraint=False)
    selected_editions = fields.ManyToManyField("models.Edition", related_name="readers", db_constraint=False)


class Edition(Model):
    number = fields.IntField()
    revision = fields.IntField()
    name = fields.TextField()
    pk = fields.CompositePrimaryKey("number", "revision")


class Book(Model):
    name = fields.TextField()
    author = fields.ForeignKeyField(Author, related_name="books", db_constraint=False)


class Review(Model):
    name = fields.TextField()
    edition = fields.ForeignKeyField(Edition, related_name="reviews", db_constraint=False)
