from __future__ import annotations

from hare import Model, fields


class SchemaTenantRecordInSchema(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        tenant_schema = True
        schema = "other"
