"""Models of the change capture tests (``Meta.change_capture``) - each captured into one outbox."""

from hare import fields
from hare.contrib.outbox import ChangeCapture, ChangeExtension, ChangePayload, OutboxEvent
from hare.fields.enums import OnDelete
from hare.instrumentation.enums import RowOperation
from hare.models import Model


class CaptureOutboxEvent(OutboxEvent):
    class Meta(OutboxEvent.Meta):
        pass


class TenantCaptureOutboxEvent(OutboxEvent):
    tenant_id = fields.IntField()

    class Meta(OutboxEvent.Meta):
        tenant_field = "tenant_id"


class CapturedCustomer(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        change_capture = ChangeCapture(CaptureOutboxEvent)


class CapturedOrder(Model):
    id = fields.IntField(primary_key=True)
    status = fields.CharField(max_length=20, default="new")
    total = fields.DecimalField(max_digits=10, decimal_places=2, default=0)
    secret = fields.CharField(max_length=50, null=True, sensitive=True)
    customer = fields.ForeignKeyField(
        "models.CapturedCustomer", related_name="orders", on_delete=OnDelete.CASCADE, null=True
    )

    class Meta:
        change_capture = ChangeCapture(CaptureOutboxEvent)


class CapturedOrderLine(Model):
    id = fields.IntField(primary_key=True)
    quantity = fields.IntField(default=1)
    order = fields.ForeignKeyField("models.CapturedOrder", related_name="lines", on_delete=OnDelete.CASCADE)

    class Meta:
        change_capture = ChangeCapture(CaptureOutboxEvent, payload=ChangePayload.KEYS)


class CapturedOrderNote(Model):
    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=50, default="")
    order = fields.ForeignKeyField(
        "models.CapturedOrder", related_name="notes", on_delete=OnDelete.SET_NULL, null=True
    )

    class Meta:
        change_capture = ChangeCapture(CaptureOutboxEvent)


class UncapturedParent(Model):
    """Not captured - its delete reaches a captured model through a database cascade."""

    id = fields.IntField(primary_key=True)


class CapturedChild(Model):
    id = fields.IntField(primary_key=True)
    parent = fields.ForeignKeyField("models.UncapturedParent", related_name="children", on_delete=OnDelete.CASCADE)

    class Meta:
        change_capture = ChangeCapture(CaptureOutboxEvent)


class AuditedOrder(Model):
    id = fields.IntField(primary_key=True)
    status = fields.CharField(max_length=20, default="new")
    note = fields.CharField(max_length=50, default="")

    class Meta:
        track_dirty_fields = True
        change_capture = ChangeCapture(CaptureOutboxEvent, payload=ChangePayload.BEFORE_AND_AFTER)


class SoftOrder(Model):
    id = fields.IntField(primary_key=True)
    status = fields.CharField(max_length=20, default="new")
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        soft_delete_field = "deleted_at"
        change_capture = ChangeCapture(CaptureOutboxEvent)


class CompositeKeyRow(Model):
    id = fields.IntField()
    version = fields.IntField()
    name = fields.CharField(max_length=50)

    pk = fields.CompositePrimaryKey("id", "version")

    class Meta:
        change_capture = ChangeCapture(CaptureOutboxEvent)


class TenantRow(Model):
    id = fields.IntField(primary_key=True)
    tenant_id = fields.IntField()
    name = fields.CharField(max_length=50)

    class Meta:
        tenant_field = "tenant_id"
        change_capture = ChangeCapture(TenantCaptureOutboxEvent)


class CapturedTag(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)


class CapturedArticle(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    tags = fields.ManyToManyField("models.CapturedTag", through="models.CapturedArticleTag", related_name="articles")


class CapturedArticleTag(Model):
    id = fields.IntField(primary_key=True)
    article = fields.ForeignKeyField("models.CapturedArticle", on_delete=OnDelete.CASCADE)
    tag = fields.ForeignKeyField("models.CapturedTag", on_delete=OnDelete.CASCADE)

    class Meta:
        change_capture = ChangeCapture(CaptureOutboxEvent, payload=ChangePayload.KEYS)


def get_row_extension(change):
    return ChangeExtension(payload={"source": "test"}, headers={"author": "alice"})


class ExtendedRow(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    hidden = fields.CharField(max_length=50, default="")

    class Meta:
        change_capture = ChangeCapture(
            CaptureOutboxEvent,
            operations=(RowOperation.INSERT, RowOperation.UPDATE),
            exclude=("hidden",),
            topic=lambda change: f"rows.{change.operation}",
            ordering_key=None,
            extend=get_row_extension,
        )


class UncapturedRow(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
