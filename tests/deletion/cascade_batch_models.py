"""Models for tests/deletion/test_cascade_batching.py - a Python-side delete cascade run in batches."""

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


class BatchConstrainedParent(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "batch_constrained_parent"


class BatchConstrainedAuditedChild(Model):
    """Overrides delete() behind a relation the database enforces - the cascade still calls it."""

    id = fields.IntField(primary_key=True)
    parent: fields.ForeignKeyRelation[BatchConstrainedParent] = fields.ForeignKeyField(
        "models.BatchConstrainedParent", related_name="audited_children", on_delete=fields.CASCADE
    )
    delete_override_calls: ClassVar[list[Any]] = []

    class Meta:
        table = "batch_constrained_audited_child"

    async def delete(self, using: Any = None) -> None:
        self.delete_override_calls.append(self.pk)
        await super().delete(using=using)


class BatchConstrainedMiddle(Model):
    id = fields.IntField(primary_key=True)
    parent: fields.ForeignKeyRelation[BatchConstrainedParent] = fields.ForeignKeyField(
        "models.BatchConstrainedParent", related_name="middles", on_delete=fields.CASCADE
    )

    class Meta:
        table = "batch_constrained_middle"


class BatchConstrainedAuditedGrandchild(Model):
    """Overrides delete() two database-enforced relations below the deleted row."""

    id = fields.IntField(primary_key=True)
    middle: fields.ForeignKeyRelation[BatchConstrainedMiddle] = fields.ForeignKeyField(
        "models.BatchConstrainedMiddle", related_name="audited_grandchildren", on_delete=fields.CASCADE
    )
    delete_override_calls: ClassVar[list[Any]] = []

    class Meta:
        table = "batch_constrained_audited_grandchild"

    async def delete(self, using: Any = None) -> None:
        self.delete_override_calls.append(self.pk)
        await super().delete(using=using)


class BatchCycleTop(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "batch_cycle_top"


class BatchCycleMiddle(Model):
    """Overrides delete() and cascades onto BatchCyclePartner, which cascades back onto it."""

    id = fields.IntField(primary_key=True)
    top: fields.ForeignKeyRelation[BatchCycleTop] = fields.ForeignKeyField(
        "models.BatchCycleTop", related_name="middles", on_delete=fields.CASCADE
    )
    partner: fields.ForeignKeyNullableRelation["BatchCyclePartner"] = fields.ForeignKeyField(
        "models.BatchCyclePartner", related_name="middles", on_delete=fields.CASCADE, null=True
    )
    delete_override_calls: ClassVar[list[Any]] = []

    class Meta:
        table = "batch_cycle_middle"

    async def delete(self, using: Any = None) -> None:
        self.delete_override_calls.append(self.pk)
        await super().delete(using=using)


class BatchCyclePartner(Model):
    """Overrides delete() and cascades onto BatchCycleMiddle, which cascades back onto it."""

    id = fields.IntField(primary_key=True)
    middle: fields.ForeignKeyNullableRelation[BatchCycleMiddle] = fields.ForeignKeyField(
        "models.BatchCycleMiddle", related_name="partners", on_delete=fields.CASCADE, null=True
    )
    delete_override_calls: ClassVar[list[Any]] = []

    class Meta:
        table = "batch_cycle_partner"

    async def delete(self, using: Any = None) -> None:
        self.delete_override_calls.append(self.pk)
        await super().delete(using=using)


class BatchSoftOwner(Model):
    id = fields.IntField(primary_key=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "batch_soft_owner"
        soft_delete_field = "deleted_at"


class BatchSoftAuditedHolding(Model):
    """Soft-deleted, overriding delete() and hard_delete() - a hard delete above it takes hard_delete()."""

    id = fields.IntField(primary_key=True)
    owner: fields.ForeignKeyRelation[BatchSoftOwner] = fields.ForeignKeyField(
        "models.BatchSoftOwner", related_name="holdings", on_delete=fields.CASCADE
    )
    deleted_at = fields.DatetimeField(null=True)
    delete_override_calls: ClassVar[list[Any]] = []
    hard_delete_override_calls: ClassVar[list[Any]] = []

    class Meta:
        table = "batch_soft_audited_holding"
        soft_delete_field = "deleted_at"

    async def delete(self, using: Any = None) -> None:
        self.delete_override_calls.append(self.pk)
        await super().delete(using=using)

    async def hard_delete(self, using: Any = None) -> None:
        self.hard_delete_override_calls.append(self.pk)
        await super().hard_delete(using=using)


class BatchAuditedHoldingItem(Model):
    """Overrides delete() below a soft-deleted model overriding its own."""

    id = fields.IntField(primary_key=True)
    holding: fields.ForeignKeyRelation[BatchSoftAuditedHolding] = fields.ForeignKeyField(
        "models.BatchSoftAuditedHolding", related_name="items", on_delete=fields.CASCADE
    )
    delete_override_calls: ClassVar[list[Any]] = []

    class Meta:
        table = "batch_audited_holding_item"

    async def delete(self, using: Any = None) -> None:
        self.delete_override_calls.append(self.pk)
        await super().delete(using=using)


class BatchSoftAuditedLeaf(Model):
    """Soft-deleted, overriding delete(), referenced by nothing - its bulk delete is a single UPDATE."""

    id = fields.IntField(primary_key=True)
    deleted_at = fields.DatetimeField(null=True)
    delete_override_calls: ClassVar[list[Any]] = []

    class Meta:
        table = "batch_soft_audited_leaf"
        soft_delete_field = "deleted_at"

    async def delete(self, using: Any = None) -> None:
        self.delete_override_calls.append(self.pk)
        await super().delete(using=using)
