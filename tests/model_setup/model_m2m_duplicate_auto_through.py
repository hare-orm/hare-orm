"""Two ManyToManyFields between the same pair of models, both with an auto-generated through table."""

from hare import fields
from hare.models import Model


class Tag(Model):
    id = fields.IntField(primary_key=True)


class Post(Model):
    id = fields.IntField(primary_key=True)
    tags = fields.ManyToManyField("models.Tag", related_name="posts")
    featured_tags = fields.ManyToManyField("models.Tag", related_name="featured_in")
