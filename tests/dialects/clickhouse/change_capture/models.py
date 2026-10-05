"""Models of the ClickHouse change capture tests - an outbox, whose key is taken from the model's series
of numbers, which a server of ClickHouse 25.1 with a ClickHouse Keeper keeps."""

from hare import fields
from hare.contrib.outbox import ChangeCapture, OutboxEvent
from hare.models import Model


class CaptureOutbox(OutboxEvent):
    """The outbox the captured models write their changes into."""

    class Meta(OutboxEvent.Meta):
        table = "capture_outbox"


class CapturedNote(Model):
    id = fields.BigIntField(primary_key=True, generated=False)
    text = fields.CharField(max_length=20)

    class Meta:
        change_capture = ChangeCapture(CaptureOutbox)


class ArchivedNote(Model):
    id = fields.BigIntField(primary_key=True, generated=False)
    text = fields.CharField(max_length=20, default="")

    class Meta:
        change_capture = ChangeCapture(CaptureOutbox)
