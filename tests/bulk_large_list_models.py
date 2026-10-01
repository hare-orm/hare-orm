"""Models for tests/test_bulk_large_lists.py - value lists and bulk writes past a backend's
bind-parameter ceiling."""

from hare import fields
from hare.models import Model


class LargeListItem(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=100)
    value = fields.IntField(null=True)

    class Meta:
        table = "large_list_item"


class LargeListUniqueItem(Model):
    id = fields.IntField(primary_key=True, generated=True)
    name = fields.CharField(max_length=100, unique=True)

    class Meta:
        table = "large_list_unique_item"


class LargeListTenantItem(Model):
    id = fields.IntField(primary_key=True)
    tenant_id = fields.IntField()
    name = fields.CharField(max_length=100)
    counter = fields.IntField(default=0)

    class Meta:
        table = "large_list_tenant_item"
        tenant_field = "tenant_id"


class LargeListPair(Model):
    a = fields.IntField()
    b = fields.IntField()
    name = fields.CharField(max_length=100, null=True)
    pk = fields.CompositePrimaryKey("a", "b")

    class Meta:
        table = "large_list_pair"


class LargeListTag(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "large_list_tag"


class LargeListPost(Model):
    id = fields.IntField(primary_key=True)
    tags = fields.ManyToManyField("models.LargeListTag", related_name="posts")

    class Meta:
        table = "large_list_post"


class LargeListLabel(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "large_list_label"


class LargeListLabelling(Model):
    id = fields.IntField(primary_key=True, generated=True)
    article = fields.ForeignKeyField("models.LargeListArticle", related_name="labellings")
    label = fields.ForeignKeyField("models.LargeListLabel", related_name="labellings")
    note = fields.CharField(max_length=20, default="-")

    class Meta:
        table = "large_list_labelling"


class LargeListArticle(Model):
    id = fields.IntField(primary_key=True)
    labels = fields.ManyToManyField(
        "models.LargeListLabel", through="models.LargeListLabelling", related_name="articles"
    )

    class Meta:
        table = "large_list_article"


class LargeListProtectedTag(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "large_list_protected_tag"


class LargeListProtectingPost(Model):
    id = fields.IntField(primary_key=True)
    tags = fields.ManyToManyField("models.LargeListProtectedTag", related_name="posts", on_delete=fields.PROTECT)

    class Meta:
        table = "large_list_protecting_post"


class LargeListProtectedPair(Model):
    a = fields.IntField()
    b = fields.IntField()
    pk = fields.CompositePrimaryKey("a", "b")

    class Meta:
        table = "large_list_protected_pair"


class LargeListPairOwner(Model):
    id = fields.IntField(primary_key=True)
    pairs = fields.ManyToManyField("models.LargeListProtectedPair", related_name="owners", on_delete=fields.PROTECT)

    class Meta:
        table = "large_list_pair_owner"


class LargeListParent(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "large_list_parent"


class LargeListChild(Model):
    id = fields.IntField(primary_key=True)
    parent = fields.ForeignKeyField("models.LargeListParent", related_name="children")

    class Meta:
        table = "large_list_child"


class LargeListDefaultsOnly(Model):
    id = fields.IntField(primary_key=True, generated=True)
    price = fields.IntField(db_default=3)

    class Meta:
        table = "large_list_defaults_only"


class LargeListAbstractThing(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        abstract = True


class LargeListThingA(LargeListAbstractThing):
    class Meta:
        table = "large_list_thing_a"


class LargeListThingB(LargeListAbstractThing):
    class Meta:
        table = "large_list_thing_b"
