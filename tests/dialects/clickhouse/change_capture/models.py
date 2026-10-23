"""Models of the ClickHouse change capture test - an outbox, whose unique fields ClickHouse keeps nothing of."""

from hare import fields
from hare.contrib.outbox import ChangeCapture, OutboxEvent
from hare.models import Model


class CaptureOutbox(OutboxEvent):
    """The outbox the captured model writes its changes into."""

    class Meta(OutboxEvent.Meta):
        table = "capture_outbox"


class CapturedNote(Model):
    id = fields.BigIntField(primary_key=True, generated=False)
    text = fields.CharField(max_length=20)

    class Meta:
        change_capture = ChangeCapture(CaptureOutbox)
