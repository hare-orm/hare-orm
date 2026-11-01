from __future__ import annotations

from hare import Model, fields
from hare.contrib.outbox import ChangeCapture, OutboxEvent
from hare.contrib.versioning import VersionedModel
from hare.ddl import Policy, RowLevelSecurity, TenantCondition
from hare.fields import CompositePrimaryKey


class RlsEvent(OutboxEvent):
    class Meta(OutboxEvent.Meta):
        table = "rls_event"


class RlsDocument(VersionedModel):
    company_id = fields.IntField()
    title = fields.CharField(max_length=50)

    class Meta(VersionedModel.Meta):
        table = "rls_document"
        tenant_field = "company_id"
        row_level_security = RowLevelSecurity.FORCED
        policies = [Policy(name="rls_document_tenant", using=TenantCondition())]
        change_capture = ChangeCapture(RlsEvent)


class RlsLine(Model):
    order_id = fields.IntField()
    number = fields.IntField()
    company_id = fields.IntField()
    deleted_at = fields.DatetimeField(null=True)
    pk = CompositePrimaryKey("order_id", "number")

    class Meta:
        table = "rls_line"
        tenant_field = "company_id"
        soft_delete_field = "deleted_at"
        row_level_security = RowLevelSecurity.FORCED
        policies = [Policy(name="rls_line_tenant", using=TenantCondition())]
        change_capture = ChangeCapture(RlsEvent)
