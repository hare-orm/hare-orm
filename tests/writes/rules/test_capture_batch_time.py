from types import SimpleNamespace

import pytest

from hare import Model, fields
from hare.dialects.dialect_registry import DialectRegistry
from hare.instrumentation.capture.capture_needs import CaptureNeeds
from hare.instrumentation.capture.captured_write import CapturedWrite
from hare.instrumentation.enums import ChangePayload, RowOperation

pytestmark = pytest.mark.database_independent


def test_returned_batches_share_the_time_of_the_first_change():
    class Book(Model):
        id = fields.IntField(primary_key=True)
        title = fields.TextField()

    needs = CaptureNeeds(
        sink=SimpleNamespace(check_connection=lambda connection: None),
        payload=ChangePayload.BEFORE_AND_AFTER,
        operations=frozenset(RowOperation),
        field_names=("id", "title"),
        columns=("id", "title"),
        tenant_field_name=None,
    )
    connection = SimpleNamespace(dialect=DialectRegistry.get_dialect("sqlite"))
    captured = CapturedWrite(Book, needs, connection, RowOperation.UPDATE)
    (first,) = captured.get_returned_changes(
        [{"id": 1, "title": "after"}], before_by_pk={1: {"id": 1, "title": "before"}}
    )
    (second,) = captured.get_returned_changes(
        [{"id": 2, "title": "later"}], before_by_pk={2: {"id": 2, "title": "earlier"}}
    )
    assert first.before == {"id": 1, "title": "before"}
    assert second.before == {"id": 2, "title": "earlier"}
    assert first.occurred_at is second.occurred_at
