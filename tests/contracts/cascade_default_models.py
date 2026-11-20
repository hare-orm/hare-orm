from hare import fields
from hare.models import Model
from tests.contracts.default_key_values import DefaultKeyValues


class SyncCascadeDefaultTarget(Model):
    region = fields.IntField()
    number = fields.IntField()
    deleted_at = fields.DatetimeField(null=True)
    pk = fields.CompositePrimaryKey("region", "number")

    class Meta:
        soft_delete_field = "deleted_at"


class AsyncCascadeDefaultTarget(Model):
    region = fields.IntField()
    number = fields.IntField()
    deleted_at = fields.DatetimeField(null=True)
    pk = fields.CompositePrimaryKey("region", "number")

    class Meta:
        soft_delete_field = "deleted_at"


class SyncScalarDefaultTarget(Model):
    id = fields.IntField(primary_key=True, generated=False)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        soft_delete_field = "deleted_at"


class AsyncScalarDefaultTarget(Model):
    id = fields.IntField(primary_key=True, generated=False)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        soft_delete_field = "deleted_at"


class SyncCascadeChild(Model):
    id = fields.IntField(primary_key=True, generated=False)
    parent = fields.ForeignKeyField(
        "models.SyncCascadeDefaultTarget",
        default=DefaultKeyValues.get_key,
        null=True,
        db_constraint=False,
        on_delete=fields.SET_DEFAULT,
    )


class AsyncCascadeChild(Model):
    id = fields.IntField(primary_key=True, generated=False)
    parent = fields.ForeignKeyField(
        "models.AsyncCascadeDefaultTarget",
        default=DefaultKeyValues.get_async_key,
        null=True,
        db_constraint=False,
        on_delete=fields.SET_DEFAULT,
    )


class SyncScalarCascadeChild(Model):
    id = fields.IntField(primary_key=True, generated=False)
    parent = fields.ForeignKeyField(
        "models.SyncScalarDefaultTarget",
        default=DefaultKeyValues.get_scalar_key,
        null=True,
        db_constraint=False,
        on_delete=fields.SET_DEFAULT,
    )


class AsyncScalarCascadeChild(Model):
    id = fields.IntField(primary_key=True, generated=False)
    parent = fields.ForeignKeyField(
        "models.AsyncScalarDefaultTarget",
        default=DefaultKeyValues.get_async_scalar_key,
        null=True,
        db_constraint=False,
        on_delete=fields.SET_DEFAULT,
    )


__models__ = [
    SyncCascadeDefaultTarget,
    AsyncCascadeDefaultTarget,
    SyncScalarDefaultTarget,
    AsyncScalarDefaultTarget,
    SyncCascadeChild,
    AsyncCascadeChild,
    SyncScalarCascadeChild,
    AsyncScalarCascadeChild,
]
