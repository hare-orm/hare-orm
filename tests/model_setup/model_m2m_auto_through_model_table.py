"""An auto-generated M2M through table name that is already a model's own table."""

from hare import fields
from hare.models import Model


class Tag(Model):
    id = fields.IntField(primary_key=True)


class Post(Model):
    id = fields.IntField(primary_key=True)
    tags = fields.ManyToManyField("models.Tag", related_name="posts")


class PostTag(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "post_tag"
