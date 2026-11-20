from hare import fields
from hare.models import Model
from tests.contracts.default_key_values import DefaultKeyValues
from tests.utils.database_under_test import DatabaseUnderTest


class DefaultTarget(Model):
    region = fields.IntField()
    number = fields.IntField()
    pk = fields.CompositePrimaryKey("region", "number")


class SyncDefaultChild(Model):
    id = fields.IntField(primary_key=True, generated=False)
    parent = fields.ForeignKeyField(
        "models.DefaultTarget", default=DefaultKeyValues.get_key, null=True, db_constraint=False
    )


class AsyncDefaultChild(Model):
    id = fields.IntField(primary_key=True, generated=False)
    parent = fields.ForeignKeyField(
        "models.DefaultTarget", default=DefaultKeyValues.get_async_key, null=True, db_constraint=False
    )


class SyncDefaultDetail(Model):
    id = fields.IntField(primary_key=True, generated=False)
    parent = fields.OneToOneField(
        "models.DefaultTarget", default=DefaultKeyValues.get_key, null=True, db_constraint=False
    )


class AsyncDefaultDetail(Model):
    id = fields.IntField(primary_key=True, generated=False)
    parent = fields.OneToOneField(
        "models.DefaultTarget", default=DefaultKeyValues.get_async_key, null=True, db_constraint=False
    )


class AsyncDefaultRecord(Model):
    id = fields.IntField(primary_key=True, generated=False)
    number = fields.IntField(default=DefaultKeyValues.get_number)
    token = fields.UUIDField(default=DefaultKeyValues.get_token)


class AsyncDefaultKeyRecord(Model):
    id = fields.UUIDField(primary_key=True, default=DefaultKeyValues.get_token)


__models__ = [DefaultTarget, SyncDefaultChild, AsyncDefaultChild, AsyncDefaultRecord, AsyncDefaultKeyRecord]
if DatabaseUnderTest.get_dialect().features.supports_unique_constraints:
    __models__ += [SyncDefaultDetail, AsyncDefaultDetail]
