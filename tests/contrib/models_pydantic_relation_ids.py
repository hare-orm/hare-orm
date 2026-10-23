import uuid

from hare import fields
from hare.models import Model


class RelationIdOwner(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=64)

    class Meta:
        table = "relation_id_owner"


class RelationIdUuidTarget(Model):
    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    label = fields.CharField(max_length=64, default="")

    class Meta:
        table = "relation_id_uuid_target"


class RelationIdCodeTarget(Model):
    code = fields.CharField(max_length=16, primary_key=True)
    label = fields.CharField(max_length=64, default="")

    class Meta:
        table = "relation_id_code_target"


class RelationIdTag(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=32)
    owner: fields.ForeignKeyNullableRelation[RelationIdOwner] = fields.ForeignKeyField(
        "models.RelationIdOwner", null=True, related_name="tags"
    )

    class Meta:
        table = "relation_id_tag"


class RelationIdItem(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=64)
    owner: fields.ForeignKeyRelation[RelationIdOwner] = fields.ForeignKeyField(
        "models.RelationIdOwner", related_name="items"
    )
    reviewer: fields.ForeignKeyNullableRelation[RelationIdOwner] = fields.ForeignKeyField(
        "models.RelationIdOwner", null=True, related_name="reviewed_items"
    )
    uuid_target: fields.ForeignKeyNullableRelation[RelationIdUuidTarget] = fields.ForeignKeyField(
        "models.RelationIdUuidTarget", null=True, related_name="items"
    )
    code_target: fields.ForeignKeyNullableRelation[RelationIdCodeTarget] = fields.ForeignKeyField(
        "models.RelationIdCodeTarget", null=True, related_name="items", source_field="code_ref"
    )
    auditor: fields.ForeignKeyNullableRelation[RelationIdOwner] = fields.ForeignKeyField(
        "models.RelationIdOwner", null=True, related_name="audited_items", sensitive=True
    )
    tags: fields.ManyToManyRelation[RelationIdTag] = fields.ManyToManyField(
        "models.RelationIdTag", related_name="items", through="relation_id_item_tag"
    )
    created = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "relation_id_item"


class RelationIdProfile(Model):
    id = fields.IntField(primary_key=True)
    bio = fields.CharField(max_length=64, default="")
    owner: fields.OneToOneRelation[RelationIdOwner] = fields.OneToOneField(
        "models.RelationIdOwner", related_name="profile"
    )
    backup_owner: fields.OneToOneNullableRelation[RelationIdOwner] = fields.OneToOneField(
        "models.RelationIdOwner", null=True, related_name="backup_profile"
    )

    class Meta:
        table = "relation_id_profile"


class RelationIdOwnerDetail(Model):
    owner: fields.OneToOneRelation[RelationIdOwner] = fields.OneToOneField(
        "models.RelationIdOwner", related_name="detail", primary_key=True
    )
    note = fields.CharField(max_length=64, default="")

    class Meta:
        table = "relation_id_owner_detail"
