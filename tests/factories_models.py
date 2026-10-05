"""Models of the tests of ``hare.contrib.factories``."""

from hare import fields
from hare.fields import CASCADE
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.models import Model


class Team(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "factory_team"


class Member(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    email = fields.CharField(max_length=100)
    is_staff = fields.BooleanField(default=False)
    team = fields.ForeignKeyField("models.Team", related_name="members", on_delete=CASCADE)

    class Meta:
        table = "factory_member"


class Label(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "factory_label"


class Article(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=100)
    author = fields.ForeignKeyField("models.Member", related_name="articles", on_delete=CASCADE)
    labels = fields.ManyToManyField("models.Label", related_name="articles")

    class Meta:
        table = "factory_article"


class Version(Model):
    pk = CompositePrimaryKey("article_number", "number")
    article_number = fields.IntField()
    number = fields.IntField()
    body = fields.CharField(max_length=50, default="")

    class Meta:
        table = "factory_version"


class Note(Model):
    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=50, default="")
    target = fields.GenericForeignKeyField({"team": Team, "version": Version}, related_name="notes", on_delete=CASCADE)

    class Meta:
        table = "factory_note"


class StoreItem(Model):
    id = fields.IntField(primary_key=True)
    store = fields.CharField(max_length=20)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "factory_store_item"
        tenant_field = "store"
