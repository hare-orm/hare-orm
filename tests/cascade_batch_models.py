"""Models for tests/test_cascade_batching.py - a Python-side delete cascade run in batches."""

from typing import Any, ClassVar

from hare import fields
from hare.models import Model


class BatchTag(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "batch_tag"


class BatchSoftParent(Model):
    id = fields.IntField(primary_key=True)
    deleted_at = fields.DatetimeField(null=True)
    version = fields.IntField(default=1)
    tags: fields.ManyToManyRelation[BatchTag] = fields.ManyToManyField(
        "models.BatchTag", related_name="soft_parents", on_delete=fields.CASCADE
    )

    class Meta:
        table = "batch_soft_parent"
        soft_delete_field = "deleted_at"
        optimistic_lock_field = "version"


class BatchSoftChild(Model):
    id = fields.IntField(primary_key=True)
    parent: fields.ForeignKeyRelation[BatchSoftParent] = fields.ForeignKeyField(
        "models.BatchSoftParent", related_name="children", on_delete=fields.CASCADE
    )
    deleted_at = fields.DatetimeField(null=True)
    version = fields.IntField(default=1)

    class Meta:
        table = "batch_soft_child"
        soft_delete_field = "deleted_at"
        optimistic_lock_field = "version"


class BatchSoftGrandchild(Model):
    id = fields.IntField(primary_key=True)
    child: fields.ForeignKeyRelation[BatchSoftChild] = fields.ForeignKeyField(
        "models.BatchSoftChild", related_name="grandchildren", on_delete=fields.CASCADE
    )
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "batch_soft_grandchild"
        soft_delete_field = "deleted_at"


class BatchSoftPairChild(Model):
    a = fields.IntField()
    b = fields.IntField()
    parent: fields.ForeignKeyRelation[BatchSoftParent] = fields.ForeignKeyField(
        "models.BatchSoftParent", related_name="pair_children", on_delete=fields.CASCADE
    )
    deleted_at = fields.DatetimeField(null=True)
    pk = fields.CompositePrimaryKey("a", "b")

    class Meta:
        table = "batch_soft_pair_child"
        soft_delete_field = "deleted_at"


class BatchHardChildOfSoft(Model):
    """No soft delete - a soft-deleted parent really deletes it."""

    id = fields.IntField(primary_key=True)
    parent: fields.ForeignKeyRelation[BatchSoftParent] = fields.ForeignKeyField(
        "models.BatchSoftParent", related_name="hard_children", on_delete=fields.CASCADE
    )

    class Meta:
        table = "batch_hard_child_of_soft"


class BatchNullingRow(Model):
    id = fields.IntField(primary_key=True)
    parent: fields.ForeignKeyNullableRelation[BatchSoftParent] = fields.ForeignKeyField(
        "models.BatchSoftParent", related_name="nulling_rows", on_delete=fields.SET_NULL, null=True
    )

    class Meta:
        table = "batch_nulling_row"


class BatchLooseParent(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "batch_loose_parent"


class BatchLooseChild(Model):
    id = fields.IntField(primary_key=True)
    parent: fields.ForeignKeyRelation[BatchLooseParent] = fields.ForeignKeyField(
        "models.BatchLooseParent", related_name="children", on_delete=fields.CASCADE, db_constraint=False
    )

    class Meta:
        table = "batch_loose_child"


class BatchLooseGrandchild(Model):
    """Reached through a database-enforced CASCADE from a Python-deleted row."""

    id = fields.IntField(primary_key=True)
    child: fields.ForeignKeyRelation[BatchLooseChild] = fields.ForeignKeyField(
        "models.BatchLooseChild", related_name="grandchildren", on_delete=fields.CASCADE
    )

    class Meta:
        table = "batch_loose_grandchild"


class BatchLooseSoftChild(Model):
    """A soft-delete row a hard delete reaches through a db_constraint=False edge."""

    id = fields.IntField(primary_key=True)
    parent: fields.ForeignKeyRelation[BatchLooseParent] = fields.ForeignKeyField(
        "models.BatchLooseParent", related_name="soft_children", on_delete=fields.CASCADE, db_constraint=False
    )
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "batch_loose_soft_child"
        soft_delete_field = "deleted_at"


class BatchLooseNullingRow(Model):
    id = fields.IntField(primary_key=True)
    child: fields.ForeignKeyNullableRelation[BatchLooseChild] = fields.ForeignKeyField(
        "models.BatchLooseChild",
        related_name="nulling_rows",
        on_delete=fields.SET_NULL,
        null=True,
        db_constraint=False,
    )

    class Meta:
        table = "batch_loose_nulling_row"


class BatchLooseRestrictor(Model):
    id = fields.IntField(primary_key=True)
    child: fields.ForeignKeyRelation[BatchLooseChild] = fields.ForeignKeyField(
        "models.BatchLooseChild", related_name="restrictors", on_delete=fields.RESTRICT, db_constraint=False
    )

    class Meta:
        table = "batch_loose_restrictor"


class BatchLooseProtector(Model):
    id = fields.IntField(primary_key=True)
    child: fields.ForeignKeyRelation[BatchLooseChild] = fields.ForeignKeyField(
        "models.BatchLooseChild", related_name="protectors", on_delete=fields.PROTECT, db_constraint=False
    )

    class Meta:
        table = "batch_loose_protector"


class BatchAuditedChild(Model):
    """Overrides delete() - the cascade has to call it for every row."""

    id = fields.IntField(primary_key=True)
    parent: fields.ForeignKeyRelation[BatchLooseParent] = fields.ForeignKeyField(
        "models.BatchLooseParent", related_name="audited_children", on_delete=fields.CASCADE, db_constraint=False
    )
    delete_override_calls: ClassVar[list[Any]] = []

    class Meta:
        table = "batch_audited_child"

    async def delete(self, using: Any = None) -> None:
        self.delete_override_calls.append(self.pk)
        await super().delete(using=using)


class BatchTenantParent(Model):
    id = fields.IntField(primary_key=True)
    company_id = fields.IntField()
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "batch_tenant_parent"
        tenant_field = "company_id"
        soft_delete_field = "deleted_at"


class BatchTenantChild(Model):
    id = fields.IntField(primary_key=True)
    company_id = fields.IntField()
    parent: fields.ForeignKeyRelation[BatchTenantParent] = fields.ForeignKeyField(
        "models.BatchTenantParent", related_name="children", on_delete=fields.CASCADE
    )
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "batch_tenant_child"
        tenant_field = "company_id"
        soft_delete_field = "deleted_at"
