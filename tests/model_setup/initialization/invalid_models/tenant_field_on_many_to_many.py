"""Bad model - Meta.tenant_field naming a ManyToManyField, which has no column of its own."""

from hare import fields
from hare.models import Model


class TenantFieldM2MTag(Model):
    id = fields.IntField(primary_key=True)


class TenantFieldM2MOwner(Model):
    id = fields.IntField(primary_key=True)
    tags: fields.ManyToManyRelation[TenantFieldM2MTag] = fields.ManyToManyField("models.TenantFieldM2MTag")

    class Meta:
        tenant_field = "tags"
