from __future__ import annotations

from hare import Model, fields
from hare.contrib.outbox import ChangeCapture, OutboxEvent
from hare.contrib.versioning import VersionedModel
from hare.fields import CompositePrimaryKey


class ScopeShop(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "scope_shop"


class ScopeOrder(Model):
    """A shared table divided by a tenant column."""

    id = fields.IntField(primary_key=True)
    company = fields.IntField()

    class Meta:
        table = "scope_order"
        tenant_field = "company"


class ScopeNote(Model):
    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=50)
    shop = fields.ForeignKeyField("models.ScopeShop", related_name="notes", null=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "scope_note"
        tenant_schema = True
        soft_delete_field = "deleted_at"


class ScopeTask(Model):
    id = fields.IntField(primary_key=True)
    note = fields.ForeignKeyField("models.ScopeNote", related_name="tasks", null=True)

    class Meta:
        table = "scope_task"
        tenant_schema = True


class ScopeTenantBase(Model):
    class Meta:
        abstract = True
        tenant_schema = True


class ScopeMemo(ScopeTenantBase):
    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=50)

    class Meta:
        table = "scope_memo"


class ScopeEvent(OutboxEvent):
    class Meta(OutboxEvent.Meta):
        table = "scope_event"
        tenant_schema = True


class ScopeDocument(VersionedModel):
    title = fields.CharField(max_length=50)

    class Meta(VersionedModel.Meta):
        table = "scope_document"
        tenant_schema = True
        change_capture = ChangeCapture(ScopeEvent)


class ScopeLine(Model):
    order_id = fields.IntField()
    number = fields.IntField()
    amount = fields.IntField(default=0)
    pk = CompositePrimaryKey("order_id", "number")

    class Meta:
        table = "scope_line"
        tenant_schema = True
        change_capture = ChangeCapture(ScopeEvent)
