from __future__ import annotations

from hare import Model, fields
from hare.ddl import Policy, TenantCondition


class RlsNoTenantField(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        policies = [Policy(name="rls_no_tenant_field", using=TenantCondition())]
