import uuid

from hare import fields
from hare.models import Model


async def make_uuid():
    return uuid.uuid4()


class AsyncPkTarget(Model):
    id = fields.UUIDField(primary_key=True, default=make_uuid)

    class Meta:
        table = "async_pk_target"


class AsyncPkChild(Model):
    id = fields.IntField(primary_key=True)
    target: fields.ForeignKeyNullableRelation[AsyncPkTarget] = fields.ForeignKeyField(
        "models.AsyncPkTarget", null=True
    )

    class Meta:
        table = "async_pk_child"
