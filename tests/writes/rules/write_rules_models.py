"""Models of the write rules tests - one model carrying every rule a row write applies: an ``auto_now`` and an
``auto_now_add`` stamp, a version, a soft delete, a sensitive field and an encrypted field with a blind index;
a through model of a many-to-many relation carrying them too; a captured model and a tenant-scoped one."""

from hare import fields
from hare.contrib.outbox import ChangeCapture, ChangePayload, OutboxEvent
from hare.ddl.constraints import UniqueConstraint
from hare.fields.encrypted import EncryptedTextField
from hare.fields.enums import OnDelete
from hare.models import Model


class RuleOutboxEvent(OutboxEvent):
    class Meta(OutboxEvent.Meta):
        pass


class RuleDocument(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50, db_default="")
    secret = fields.CharField(max_length=50, null=True, sensitive=True)
    email = EncryptedTextField(blind_index=True, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    version = fields.IntField(db_default=0)
    deleted_at = fields.DatetimeField(null=True)
    tags = fields.ManyToManyField("models.RuleTag", through="models.RuleDocumentTag", related_name="documents")
    reviewers = fields.ManyToManyField(
        "models.RuleLabel", through="models.RuleDocumentReviewer", related_name="reviewed_documents"
    )

    class Meta:
        optimistic_lock_field = "version"
        soft_delete_field = "deleted_at"


class RuleTag(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)


class RuleDocumentTag(Model):
    id = fields.IntField(primary_key=True)
    document = fields.ForeignKeyField("models.RuleDocument", on_delete=OnDelete.CASCADE)
    tag = fields.ForeignKeyField("models.RuleTag", on_delete=OnDelete.CASCADE)
    note = fields.CharField(max_length=50, null=True, sensitive=True)
    label = EncryptedTextField(blind_index=True, null=True)
    linked_at = fields.DatetimeField(auto_now=True)
    source = fields.ForeignKeyField(
        "models.RuleLabel", null=True, on_delete=OnDelete.SET_NULL, related_name="sourced_links"
    )


class RuleDocumentReviewer(Model):
    """A through model with a relation nothing fills - add() takes it in through_defaults."""

    id = fields.IntField(primary_key=True)
    document = fields.ForeignKeyField("models.RuleDocument", on_delete=OnDelete.CASCADE)
    reviewer = fields.ForeignKeyField("models.RuleLabel", on_delete=OnDelete.CASCADE)
    assigned_by = fields.ForeignKeyField("models.RuleTag", on_delete=OnDelete.CASCADE, related_name="assignments")


class RuleCapturedDocument(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50, default="")
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        change_capture = ChangeCapture(RuleOutboxEvent, payload=ChangePayload.KEYS)


class RuleTenantDocument(Model):
    id = fields.IntField(primary_key=True)
    company = fields.CharField(max_length=20)
    title = fields.CharField(max_length=50, default="")
    labels = fields.ManyToManyField(
        "models.RuleLabel", through="models.RuleTenantLabelLink", related_name="tenant_documents"
    )

    class Meta:
        tenant_field = "company"


class RuleCapturedPair(Model):
    a = fields.IntField()
    b = fields.IntField()
    note = fields.CharField(max_length=20, default="")
    pk = fields.CompositePrimaryKey("a", "b")

    class Meta:
        change_capture = ChangeCapture(RuleOutboxEvent, payload=ChangePayload.BEFORE_AND_AFTER)
        track_dirty_fields = True


class RuleTenantNote(Model):
    id = fields.IntField(primary_key=True)
    document = fields.ForeignKeyField("models.RuleTenantDocument", related_name="notes", null=True)


class RuleLabel(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)


class RuleTenantLabelLink(Model):
    """A link of a tenant, soft-deleted and unique by its pair and tenant - add() restores it."""

    id = fields.IntField(primary_key=True)
    document = fields.ForeignKeyField("models.RuleTenantDocument", on_delete=OnDelete.CASCADE)
    label = fields.ForeignKeyField("models.RuleLabel", on_delete=OnDelete.CASCADE)
    company = fields.CharField(max_length=20)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        tenant_field = "company"
        soft_delete_field = "deleted_at"
        constraints = [UniqueConstraint(fields=("document", "label", "company"), name="rule_tenant_label_link")]
