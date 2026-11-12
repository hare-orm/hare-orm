"""Tenant-schema models with both key shapes and relations, used to check client selection."""

from hare import fields
from hare.models import Model


class SchemaNote(Model):
    id = fields.IntField(primary_key=True, generated=False)
    text = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    selected_editions = fields.ManyToManyField("models.SchemaEdition", related_name="readers", db_constraint=False)

    class Meta:
        tenant_schema = True
        soft_delete_field = "deleted_at"


class SchemaEdition(Model):
    number = fields.IntField()
    revision = fields.IntField()
    text = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    note = fields.ForeignKeyField("models.SchemaNote", related_name="editions", null=True, db_constraint=False)
    pk = fields.CompositePrimaryKey("number", "revision")

    class Meta:
        tenant_schema = True
        soft_delete_field = "deleted_at"
