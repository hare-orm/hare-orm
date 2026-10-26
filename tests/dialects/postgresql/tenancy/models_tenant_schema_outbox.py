from __future__ import annotations

from hare import Model, fields
from hare.contrib.outbox import ChangeCapture, OutboxEvent


class TenantSchemaEvent(OutboxEvent):
    class Meta(OutboxEvent.Meta):
        table = "tenant_schema_event"
        tenant_schema = True


class SharedCapturedRow(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "shared_captured_row"
        change_capture = ChangeCapture(TenantSchemaEvent)
