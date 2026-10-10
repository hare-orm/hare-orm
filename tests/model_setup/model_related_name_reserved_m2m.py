"""A ManyToManyField whose related_name shadows a method of the target model itself."""

from hare import fields
from hare.models import Model


class Tag(Model):
    id = fields.IntField(primary_key=True)

    def display_name(self) -> str:
        return f"tag {self.id}"


class Post(Model):
    id = fields.IntField(primary_key=True)
    tags = fields.ManyToManyField("models.Tag", related_name="display_name")
