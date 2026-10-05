from __future__ import annotations

from hare import Model, fields


class SchemaTenantRecord(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        tenant_schema = True


class SharedRecordOwner(Model):
    id = fields.IntField(primary_key=True)
    record = fields.ForeignKeyField("models.SchemaTenantRecord")
