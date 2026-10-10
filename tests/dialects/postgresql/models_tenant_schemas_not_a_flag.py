from __future__ import annotations

from hare import Model, fields


class SchemaTenantRecordNotAFlag(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        tenant_schema = "yes"
