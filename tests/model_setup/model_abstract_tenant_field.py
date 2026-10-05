"""Companion models for tests/model_setup/test_abstract_model_inheritance.py.

Meta.tenant_field declared on an abstract base, inherited by two different concrete siblings -
each concrete subclass must get its own correctly independent MetaInfo.tenant_field, and
Tenancy.scope() must correctly scope BOTH siblings' default-manager queries simultaneously
without cross-model interference."""

from hare import fields
from hare.models import Model


class AbstractTenantBase(Model):
    company_id = fields.IntField()
    name = fields.CharField(50)

    class Meta:
        abstract = True
        tenant_field = "company_id"


class TenantSiblingX(AbstractTenantBase):
    pass


class TenantSiblingY(AbstractTenantBase):
    pass
