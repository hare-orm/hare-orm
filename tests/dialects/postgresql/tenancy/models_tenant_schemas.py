from __future__ import annotations

from hare import Model, fields


class TenantCompany(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "tenant_company"


class TenantNote(Model):
    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=50)
    company = fields.ForeignKeyField("models.TenantCompany", related_name="notes", null=True)

    class Meta:
        table = "tenant_note"
        tenant_schema = True
