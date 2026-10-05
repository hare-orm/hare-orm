from __future__ import annotations

from hare import Model, fields
from hare.ddl import Policy, RowLevelSecurity, TenantCondition


class RlsNote(Model):
    id = fields.IntField(primary_key=True)
    company_id = fields.IntField()
    text = fields.CharField(max_length=50)

    class Meta:
        table = "rls_note"
        tenant_field = "company_id"
        row_level_security = RowLevelSecurity.FORCED
        policies = [Policy(name="rls_note_tenant", using=TenantCondition())]


class RlsLabel(Model):
    id = fields.IntField(primary_key=True)
    company_code = fields.CharField(max_length=20)

    class Meta:
        table = "rls_label"
        tenant_field = "company_code"
        row_level_security = RowLevelSecurity.FORCED
        policies = [Policy(name="rls_label_tenant", using=TenantCondition())]
