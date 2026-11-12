"""Tenant-scoped soft deletion with single and composite identities."""

from hare import fields
from hare.models import Model


class ScopedItem(Model):
    id = fields.IntField(primary_key=True, generated=False)
    tenant_id = fields.IntField()
    amount = fields.IntField()
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        tenant_field = "tenant_id"
        soft_delete_field = "deleted_at"
        track_dirty_fields = True


class CompositeScopedItem(Model):
    id = fields.IntField()
    tenant_id = fields.IntField()
    amount = fields.IntField()
    deleted_at = fields.DatetimeField(null=True)
    pk = fields.CompositePrimaryKey("tenant_id", "id")

    class Meta:
        tenant_field = "tenant_id"
        soft_delete_field = "deleted_at"
        track_dirty_fields = True
